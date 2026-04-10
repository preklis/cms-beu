/* Programming contest management system
 * Copyright © 2026
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 */

var ContestSelector = new function () {
    var self = this;

    self.init = function () {
        self.select = $("#ContestSelector_select");

        self.select.on("change", function () {
            DataStore.set_active_contest($(this).val());
        });

        DataStore.contest_create.add(self.refresh);
        DataStore.contest_update.add(self.refresh);
        DataStore.contest_delete.add(self.refresh);
        DataStore.active_contest_change.add(self.sync_value);

        self.refresh();
    };

    self.refresh = function () {
        var html = "";
        var contests = DataStore.contest_list;

        for (var i = 0; i < contests.length; i += 1) {
            var contest = contests[i];
            html += "<option value=\"" + contest.key + "\">" +
                $("<div>").text(contest.name).html() + "</option>";
        }

        self.select.html(html);
        self.sync_value();
    };

    self.sync_value = function () {
        var active = DataStore.active_contest_key;
        if (active !== null && DataStore.contests[active] !== undefined) {
            self.select.val(active);
        }
    };
};
