#!/usr/bin/env python3

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2010-2013 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2015 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
# Copyright © 2012-2018 Luca Wehrstedt <luca.wehrstedt@gmail.com>
# Copyright © 2014 Artem Iglikov <artem.iglikov@gmail.com>
# Copyright © 2014 Fabian Gundlach <320pointsguy@gmail.com>
# Copyright © 2016 Myungwoo Chun <mc.tamaki@gmail.com>
# Copyright © 2017 Valentin Rosca <rosca.valentin2012@gmail.com>
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

"""User-related handlers for AWS.

"""

import csv
import io
import re
import secrets

from cms.db import Contest, Participation, Submission, Team, User
from cmscommon.crypto import hash_password
from cmscommon.datetime import make_datetime

from .base import BaseHandler, SimpleHandler, require_permission


USER_CSV_FIELDS = [
    "First_Name",
    "Last_Name",
    "E-mail",
    "Timezone",
    "Preferred_languages",
]

CODENAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _normalize_csv_key(key):
    return key.strip().lower().replace("-", "_").replace(" ", "_")


def _get_csv_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        value = value.strip()
        if value != "":
            return value
    return None


def _parse_user_csv(body):
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("Missing CSV header.")

    users = []
    for row_number, row in enumerate(reader, start=2):
        if all((value or "").strip() == "" for value in row.values()):
            continue

        normalized_row = {
            _normalize_csv_key(key): (value or "")
            for key, value in row.items()
            if key is not None
        }

        attrs = {
            "first_name": _get_csv_value(normalized_row, "first_name") or "",
            "last_name": _get_csv_value(normalized_row, "last_name") or "",
            "email": _get_csv_value(normalized_row, "email", "e_mail"),
            "timezone": _get_csv_value(normalized_row, "timezone"),
            "preferred_languages": [],
        }

        preferred_languages = _get_csv_value(normalized_row,
                                             "preferred_languages")
        if preferred_languages is not None:
            attrs["preferred_languages"] = [
                language.strip()
                for language in preferred_languages.split(",")
                if language.strip()
            ]

        users.append(attrs)

    if len(users) == 0:
        raise ValueError("The CSV file does not contain any users.")

    return users


def _get_username_generator(pattern, taken_usernames):
    if "*" not in pattern:
        raise ValueError("Username format must contain '*' placeholder.")

    current = 1

    def allocate():
        nonlocal current
        while True:
            username = pattern.replace("*", str(current))
            current += 1

            if not username:
                raise ValueError("Generated username cannot be empty.")
            if CODENAME_RE.match(username) is None:
                raise ValueError(
                    "Generated username '%s' contains invalid characters."
                    % username)
            if username in taken_usernames:
                continue

            taken_usernames.add(username)
            return username

    return allocate


class UserHandler(BaseHandler):
    @require_permission(BaseHandler.AUTHENTICATED)
    def get(self, user_id):
        user = self.safe_get_item(User, user_id)

        self.r_params = self.render_params()
        self.r_params["user"] = user
        self.r_params["participations"] = \
            self.sql_session.query(Participation)\
                .filter(Participation.user == user)\
                .all()
        self.r_params["unassigned_contests"] = \
            self.sql_session.query(Contest)\
                .filter(Contest.id.notin_(
                    self.sql_session.query(Participation.contest_id)
                        .filter(Participation.user == user)
                        .all()))\
                .all()
        self.render("user.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, user_id):
        fallback_page = self.url("user", user_id)

        user = self.safe_get_item(User, user_id)

        try:
            attrs = user.get_attrs()

            self.get_string(attrs, "first_name")
            self.get_string(attrs, "last_name")
            self.get_string(attrs, "username", empty=None)

            self.get_password(attrs, user.password, False)

            self.get_string(attrs, "email", empty=None)
            self.get_string_list(attrs, "preferred_languages")
            self.get_string(attrs, "timezone", empty=None)

            assert attrs.get("username") is not None, \
                "No username specified."

            # Update the user.
            user.set_attrs(attrs)

        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        if self.try_commit():
            # Update the user on RWS.
            self.service.proxy_service.reinitialize()
        self.redirect(fallback_page)


class UserListHandler(SimpleHandler("users.html")):
    """Get returns the list of all users, post perform operations on
    a specific user (removing them from CMS).

    """

    REMOVE = "Remove"

    @require_permission(BaseHandler.AUTHENTICATED)
    def post(self):
        user_id = self.get_argument("user_id")
        operation = self.get_argument("operation")

        if operation == self.REMOVE:
            asking_page = self.url("users", user_id, "remove")
            # Open asking for remove page
            self.redirect(asking_page)
        else:
            self.service.add_notification(
                make_datetime(), "Invalid operation %s" % operation, "")
            self.redirect(self.url("contests"))


class RemoveUserHandler(BaseHandler):
    """Get returns a page asking for confirmation, delete actually removes
    the user from CMS.

    """

    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self, user_id):
        user = self.safe_get_item(User, user_id)
        submission_query = self.sql_session.query(Submission)\
            .join(Submission.participation)\
            .filter(Participation.user == user)
        participation_query = self.sql_session.query(Participation)\
            .filter(Participation.user == user)

        self.render_params_for_remove_confirmation(submission_query)
        self.r_params["user"] = user
        self.r_params["participation_count"] = participation_query.count()
        self.render("user_remove.html", **self.r_params)

    @require_permission(BaseHandler.PERMISSION_ALL)
    def delete(self, user_id):
        user = self.safe_get_item(User, user_id)

        self.sql_session.delete(user)
        if self.try_commit():
            self.service.proxy_service.reinitialize()

        # Maybe they'll want to do this again (for another user)
        self.write("../../users")


class TeamHandler(BaseHandler):
    """Manage a single team.

    If referred by GET, this handler will return a pre-filled HTML form.
    If referred by POST, this handler will sync the team data with the form's.
    """
    def get(self, team_id):
        team = self.safe_get_item(Team, team_id)

        self.r_params = self.render_params()
        self.r_params["team"] = team
        self.render("team.html", **self.r_params)

    def post(self, team_id):
        fallback_page = self.url("team", team_id)

        team = self.safe_get_item(Team, team_id)

        try:
            attrs = team.get_attrs()

            self.get_string(attrs, "code")
            self.get_string(attrs, "name")

            assert attrs.get("code") is not None, \
                "No team code specified."

            # Update the team.
            team.set_attrs(attrs)

        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        if self.try_commit():
            # Update the team on RWS.
            self.service.proxy_service.reinitialize()
        self.redirect(fallback_page)


class AddTeamHandler(SimpleHandler("add_team.html", permission_all=True)):
    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self):
        fallback_page = self.url("teams", "add")

        try:
            attrs = dict()

            self.get_string(attrs, "code")
            self.get_string(attrs, "name")

            assert attrs.get("code") is not None, \
                "No team code specified."

            # Create the team.
            team = Team(**attrs)
            self.sql_session.add(team)

        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        if self.try_commit():
            # Create the team on RWS.
            self.service.proxy_service.reinitialize()

        # In case other teams need to be added.
        self.redirect(fallback_page)


class AddUserHandler(SimpleHandler("add_user.html", permission_all=True)):
    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self):
        fallback_page = self.url("users", "add")

        try:
            attrs = dict()

            self.get_string(attrs, "first_name")
            self.get_string(attrs, "last_name")
            self.get_string(attrs, "username", empty=None)

            self.get_password(attrs, None, False)

            self.get_string(attrs, "email", empty=None)

            assert attrs.get("username") is not None, \
                "No username specified."

            self.get_string(attrs, "timezone", empty=None)

            self.get_string_list(attrs, "preferred_languages")

            # Create the user.
            user = User(**attrs)
            self.sql_session.add(user)

        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        if self.try_commit():
            # Create the user on RWS.
            self.service.proxy_service.reinitialize()
            self.redirect(self.url("user", user.id))
        else:
            self.redirect(fallback_page)


class UploadUsersTemplateHandler(BaseHandler):
    @require_permission(BaseHandler.PERMISSION_ALL)
    def get(self):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(USER_CSV_FIELDS)

        self.set_header("Content-Type", "text/csv; charset=utf-8")
        self.set_header("Content-Disposition",
                        "attachment; filename=\"users_template.csv\"")
        self.finish(output.getvalue())


class UploadUsersHandler(SimpleHandler("upload_users.html", permission_all=True)):
    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self):
        fallback_page = self.url("users", "upload")

        try:
            username_pattern = self.get_argument("username_pattern", "").strip()
            uploaded = self.request.files["users_csv"][0]
            if not uploaded["filename"].lower().endswith(".csv"):
                raise ValueError("The uploaded file must be a .csv file.")

            base_users = _parse_user_csv(uploaded["body"])
            taken_usernames = {
                username
                for username, in self.sql_session.query(User.username).all()
            }
            allocate_username = _get_username_generator(username_pattern,
                                                        taken_usernames)

            created = []
            for attrs in base_users:
                username = allocate_username()
                password = secrets.token_urlsafe(16)

                user_attrs = dict(attrs)
                user_attrs["username"] = username
                user_attrs["password"] = hash_password(password)

                self.sql_session.add(User(**user_attrs))
                created.append((
                    username,
                    password,
                    attrs["first_name"],
                    attrs["last_name"],
                ))

        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid CSV file", repr(error))
            self.redirect(fallback_page)
            return

        if self.try_commit():
            self.service.proxy_service.reinitialize()

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Username", "Password", "First_Name", "Last_Name"])
            for row in created:
                writer.writerow(row)

            self.set_header("Content-Type", "text/csv; charset=utf-8")
            self.set_header(
                "Content-Disposition",
                "attachment; filename=\"created_users_credentials.csv\"")
            self.finish(output.getvalue())
        else:
            self.redirect(fallback_page)


class AddParticipationHandler(BaseHandler):
    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, user_id):
        fallback_page = self.url("user", user_id)

        user = self.safe_get_item(User, user_id)

        try:
            contest_id = self.get_argument("contest_id")
            assert contest_id != "null", "Please select a valid contest"
        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        self.contest = self.safe_get_item(Contest, contest_id)

        attrs = {}
        self.get_bool(attrs, "hidden")
        self.get_bool(attrs, "unrestricted")

        # Create the participation.
        participation = Participation(contest=self.contest,
                                      user=user,
                                      hidden=attrs["hidden"],
                                      unrestricted=attrs["unrestricted"])
        self.sql_session.add(participation)

        if self.try_commit():
            # Create the user on RWS.
            self.service.proxy_service.reinitialize()

        # Maybe they'll want to do this again (for another contest).
        self.redirect(fallback_page)


class EditParticipationHandler(BaseHandler):
    @require_permission(BaseHandler.PERMISSION_ALL)
    def post(self, user_id):
        fallback_page = self.url("user", user_id)

        user = self.safe_get_item(User, user_id)

        try:
            contest_id = self.get_argument("contest_id")
            operation = self.get_argument("operation")
            assert contest_id != "null", "Please select a valid contest"
            assert operation in (
                "Remove",
            ), "Please select a valid operation"
        except Exception as error:
            self.service.add_notification(
                make_datetime(), "Invalid field(s)", repr(error))
            self.redirect(fallback_page)
            return

        self.contest = self.safe_get_item(Contest, contest_id)

        if operation == "Remove":
            # Remove the participation.
            participation = self.sql_session.query(Participation)\
                .filter(Participation.user == user)\
                .filter(Participation.contest == self.contest)\
                .first()
            self.sql_session.delete(participation)

        if self.try_commit():
            # Create the user on RWS.
            self.service.proxy_service.reinitialize()

        # Maybe they'll want to do this again (for another contest).
        self.redirect(fallback_page)
