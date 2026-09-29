#!/usr/bin/env python3

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2015 Stefano Maggiolo <s.maggiolo@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Tests for the scoring service.

"""

# We enable monkey patching to make many libraries gevent-friendly
# (for instance, urllib3, used by requests)
import gevent.monkey
gevent.monkey.patch_all()  # noqa

import json
import unittest
from unittest.mock import Mock, patch, PropertyMock

import gevent
import requests

# Needs to be first to allow for monkey patching the DB connection string.
from cmstestsuite.unit_tests.databasemixin import DatabaseMixin

from cms.service.ProxyService import ProxyExecutor, ProxyService
from cmscommon.constants import SCORE_MODE_MAX


class TestProxyService(DatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()

        patcher = patch("cms.db.Dataset.score_type_object",
                        new_callable=PropertyMock)
        self.score_type = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.score_type.max_score = 100
        self.score_type.ranking_headers = ["100"]

        patcher = patch("requests.put")
        self.requests_put = patcher.start()
        self.addCleanup(patcher.stop)
        self.requests_put.return_value.status_code = 200

        self.contest = self.add_contest()
        self.contest.score_precision = 2

        self.task = self.add_task(contest=self.contest)
        self.task.score_precision = 2
        self.task.score_mode = SCORE_MODE_MAX
        self.dataset = self.add_dataset(task=self.task)
        self.task.active_dataset = self.dataset

        self.team = self.add_team()
        self.user = self.add_user()
        self.participation = self.add_participation(user=self.user,
                                                    contest=self.contest,
                                                    team=self.team)

        self.new_sr_unscored()
        self.new_sr_scored()
        result = self.new_sr_scored()
        self.add_token(submission=result.submission)

        self.session.commit()

    def new_sr_unscored(self):
        submission = self.add_submission(task=self.task,
                                         participation=self.participation)
        result = self.add_submission_result(submission=submission,
                                            dataset=self.dataset)
        result.compilation_outcome = "ok"
        result.evaluation_outcome = "ok"
        return result

    def new_sr_scored(self):
        result = self.new_sr_unscored()
        result.score = 100
        result.score_details = dict()
        result.public_score = 50
        result.public_score_details = dict()
        result.ranking_score_details = ["100"]
        return result

    def test_startup(self):
        """Test that data is sent in the right order at startup."""
        ProxyService(0, self.contest.id)

        gevent.sleep(0.1)

        urls = [args[0] for args, _ in self.requests_put.call_args_list]

        self.assertTrue(urls[0].endswith("contests/"))
        self.assertTrue(any(urls[i].endswith("users/") for i in [1, 2, 3]))
        self.assertTrue(any(urls[i].endswith("teams/") for i in [1, 2, 3]))
        self.assertTrue(any(urls[i].endswith("tasks/") for i in [1, 2, 3]))
        self.assertTrue(urls[4].endswith("submissions/"))
        self.assertTrue(urls[5].endswith("subchanges/"))

    def sent(self, resource):
        """Return the entities PUT to the given resource, merged."""
        data = dict()
        for args, _ in self.requests_put.call_args_list:
            if args[0].endswith(resource + "/"):
                data.update(json.loads(args[1]))
        return data

    def test_late_participation(self):
        """Scores of users added after startup reach the rankings."""
        service = ProxyService(0, self.contest.id)
        gevent.sleep(0.1)

        user = self.add_user()
        participation = self.add_participation(user=user,
                                               contest=self.contest)
        submission = self.add_submission(task=self.task,
                                         participation=participation)
        result = self.add_submission_result(submission=submission,
                                            dataset=self.dataset)
        result.compilation_outcome = "ok"
        result.evaluation_outcome = "ok"
        result.score = 100
        result.score_details = dict()
        result.public_score = 50
        result.public_score_details = dict()
        result.ranking_score_details = ["100"]
        self.session.commit()

        service.submission_scored(submission.id)
        gevent.sleep(0.1)

        self.assertIn(user.username, self.sent("users"))
        self.assertIn(str(submission.id), self.sent("submissions"))

    def test_hidden_participation(self):
        """Scores of hidden users are never sent."""
        service = ProxyService(0, self.contest.id)
        gevent.sleep(0.1)

        user = self.add_user()
        participation = self.add_participation(user=user,
                                               contest=self.contest,
                                               hidden=True)
        submission = self.add_submission(task=self.task,
                                         participation=participation)
        self.session.commit()

        self.assertEqual(service.operations_for_score(submission), [])
        self.assertNotIn(user.username, self.sent("users"))

    @patch.object(ProxyExecutor, "FAILURE_WAIT", 0.01)
    def test_retry_when_ranking_down(self):
        """Data is sent again once an unreachable ranking comes back."""
        ok = self.requests_put.return_value
        self.requests_put.return_value = None
        self.requests_put.side_effect = \
            [requests.exceptions.ConnectionError("down")] * 3 \
            + [ok] * 20

        ProxyService(0, self.contest.id)
        gevent.sleep(0.3)

        self.assertIn(self.user.username, self.sent("users"))
        self.assertEqual(len(self.sent("submissions")), 2)
        self.assertEqual(len(self.sent("subchanges")), 3)

    @patch.object(ProxyExecutor, "FAILURE_WAIT", 0.01)
    def test_rejected_data_dropped(self):
        """Data rejected by the ranking is not retried forever."""
        ok = self.requests_put.return_value
        rejected = Mock(status_code=400)
        self.requests_put.return_value = None
        self.requests_put.side_effect = \
            lambda url, *args, **kwargs: \
            rejected if url.endswith("teams/") else ok

        ProxyService(0, self.contest.id)
        gevent.sleep(0.3)

        urls = [args[0] for args, _ in self.requests_put.call_args_list]
        self.assertEqual(sum(url.endswith("teams/") for url in urls), 1)
        # The other entity types are still delivered.
        self.assertIn(self.user.username, self.sent("users"))
        self.assertEqual(len(self.sent("submissions")), 2)


if __name__ == "__main__":
    unittest.main()
