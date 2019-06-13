# -*- coding: utf-8 -*-
# Copyright 2015 Cyan, Inc.
# Copyright 2016, 2017, 2018, 2019 Ciena Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import functools
import logging
import os
import sys
import time
import uuid
from pprint import pformat

from twisted.internet.defer import Deferred, inlineCallbacks, returnValue

from afkak import KafkaClient
from afkak.common import (
    OffsetRequest, PartitionUnavailableError, RetriableBrokerResponseError,
    SendRequest, TopicAndPartition,
)
from afkak.test.int.fixtures import KafkaHarness
from afkak.test.testutil import async_delay, random_string

log = logging.getLogger(__name__)

__all__ = [
    'IntegrationMixin',
    'kafka_versions',
    'stat',
]


def stat(key, value):
    print("##teamcity[buildStatisticValue key='{}' value='{}']".format(
        key, value), file=sys.stderr)


def first(deferreds):
    """Get the first result. Cancel the rest.

    :param deferreds:
        A sequence of `twisted.internet.defer.Deferred` instances.

        Passing a deferred to *first* transfers ownership: the caller must not
        add callbacks or cancel it. *first* cancels all other deferreds as soon
        as one fires.

        This sequence must not be mutated by the caller. *first* does not
        defensively copy.

    :returns: `Deferred` that fires with the result of the first deferred to
        fire or fail. Canceling this deferred cancels all of the deferreds.
    """
    def cancel_all(self):
        for d in deferreds:
            d.cancel()

    result_d = Deferred(cancel_all)

    def one_result(result, source):
        if result_d.called:
            return
        result_d.callback(result)
        for d in deferreds:
            if d is not source:
                d.cancel()

    for d in deferreds:
        d.addBoth(one_result, d)
    return result_d


def make_send_requests(msgs, topic=None, key=None):
    return [SendRequest(topic, key, msgs, None)]


def kafka_versions(*versions):
    def kafka_versions(func):
        @functools.wraps(func)
        def wrapper(self):
            kafka_version = os.environ.get('KAFKA_VERSION')

            if not kafka_version:
                self.skipTest("no kafka version specified")  # pragma: no cover
            elif 'all' not in versions and kafka_version not in versions:
                self.skipTest("unsupported kafka version")  # pragma: no cover

            return func(self)
        return wrapper
    return kafka_versions


@inlineCallbacks
def ensure_topic_creation(client, topic_name, fully_replicated=True, timeout=5):
    '''
    With the default Kafka configuration, just querying for the metadata
    for a particular topic will auto-create that topic.

    :param client: `afkak.client.KafkaClient` instance

    :param str topic_name: Topic name

    :param bool fully_replicated:
        If ``True``, check whether all partitions for the topic have been
        assigned brokers. This doesn't ensure that producing to the topic will
        succeed, though—there is a window after the partition is assigned
        before the broker can actually accept writes. In this case the broker
        will respond with a retriable error (see
        `IntegrationMixin.retry_broker_errors()`).

        If ``False``, only check that any metadata exists for the topic.

    :param timeout: Number of seconds to wait.
    '''
    start_time = time.time()
    if fully_replicated:
        check_func = client.topic_fully_replicated
    else:
        check_func = client.has_metadata_for_topic
    yield client.load_metadata_for_topics(topic_name)

    def topic_info():
        if topic_name in client.topic_partitions:
            return "Topic {} exists. Partition metadata: {}".format(
                topic_name, pformat([client.partition_meta[TopicAndPartition(topic_name, part)]
                                     for part in client.topic_partitions[topic_name]]),
            )
        else:
            return "No metadata for topic {} found.".format(topic_name)

    while not check_func(topic_name):
        yield async_delay(clock=client.reactor)
        if time.time() > start_time + timeout:
            raise Exception((
                "Timed out waiting topic {} creation after {} seconds. {}"
            ).format(topic_name, timeout, topic_info()))
        else:
            log.debug('Still waiting topic creation: %s.', topic_info())
        yield client.load_metadata_for_topics(topic_name)
    log.info('%s', topic_info())


class IntegrationMixin(object):
    """
    Mixin for tests that require a Kafka cluster.

    The `setUp()` and `tearDown()` methods bring up a Kafka cluster and create
    a topic for the test to use.

    Mix this into a subclass of `twisted.trial.unittest.TestCase`. Note that
    you must override *harness_kw* in the subclass.

    :data dict harness_kw:
        Keyword arguments for `harness`. Subclasses must set this to specify
        ``replicas`` (the number of Kafka brokers) and may specify other
        arguments — see `afkak.fixtures.KafkaHarness.start()`.

    :data dict client_kw:
        Keyword arguments for `client`. Subclasses may inject keyword arguments
        by overriding this. The default is empty.

    :ivar str topic:
        Kafka topic name. This may be set in subclasses. If ``None``, a random
        topic name is generated by `setUp()`.

    :ivar harness:
        `afkak.test.fixtures.KafkaHarness` instance. This is created by the
        `setUp()` method and automatically torn down.

    :ivar client:
        `afkak.KafkaClient` instance created by the `setUp()` method.

    :ivar reactor: Twisted reactor.
    """
    topic = None
    from twisted.internet import reactor
    client_kw = {}

    if not os.environ.get('KAFKA_VERSION'):  # pragma: no cover
        skip = 'KAFKA_VERSION is not set'

    @inlineCallbacks
    def setUp(self):
        log.info("Setting up test %s", self.id())

        self.harness = KafkaHarness.start(**self.harness_kw)
        self.addCleanup(self.harness.halt)

        if not self.topic:
            self.topic = "%s-%s" % (
                self.id()[self.id().rindex(".") + 1:], random_string(10))

        self.client = KafkaClient(
            self.harness.bootstrap_hosts,
            clientId=self.__class__.__name__,
            **self.client_kw
        )
        self.addCleanup(self.client.close)

        yield ensure_topic_creation(self.client, self.topic,
                                    fully_replicated=True)

        self._messages = {}

    def tearDown(self):
        log.info("Tearing down test: %r", self)

    @inlineCallbacks
    def current_offset(self, topic, partition):
        offsets, = yield self.client.send_offset_request(
            [OffsetRequest(topic, partition, -1, 1)])
        returnValue(offsets.offsets[0])

    @inlineCallbacks
    def retry_while_broker_errors(self, f, *a, **kw):
        """
        Call a function, retrying on retriable broker errors.

        If calling the function fails with one of these exception types it is
        called again after a short delay:

        * `afkak.common.RetriableBrokerResponseError` (or a subclass thereof)
        * `afkak.common.PartitionUnavailableError`

        The net effect is to keep trying until topic auto-creation completes.

        :param f: callable, which may return a `Deferred`
        :param a: arbitrary positional arguments
        :param kw: arbitrary keyword arguments
        """
        while True:
            try:
                returnValue((yield f(*a, **kw)))
                break
            except (RetriableBrokerResponseError, PartitionUnavailableError):
                yield async_delay(0.1, clock=self.reactor)

    def msg(self, s):
        if s not in self._messages:
            self._messages[s] = (u'%s-%s-%s' % (s, self.id(), uuid.uuid4())).encode('utf-8')

        return self._messages[s]
