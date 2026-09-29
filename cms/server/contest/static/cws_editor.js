/* Contest Management System - BEU fork
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 */

"use strict";

/**
 * In-browser code editor of the task submissions page.
 *
 * It lets contestants write their solution in the browser, keeps a
 * draft of it in localStorage (so that a reload or a crash of the
 * browser does not lose the code), submits it, and - when user tests
 * are enabled - compiles and runs it on a custom input using the user
 * test machinery of CMS, showing the output inline.
 *
 * The page provides the configuration as JSON in #cws-editor-config
 * and the translated strings as JSON in #cws-editor-strings.
 */

var CMS = CMS || {};

CMS.CodeEditor = function (config, strings) {
    this.cfg = config;
    this.str = strings;
    this.docs = {};
    this.current_file = config.files[0];
    this.busy = false;
    this.save_timer = null;
    this.status_timer = null;
    this.poll_timer = null;
    this.storage_prefix = ["cms-editor", config.contest, config.user,
                           config.task].join("/") + "/";
};

// Status codes of UserTestResult.get_status().
CMS.CodeEditor.TEST_COMPILING = 1;
CMS.CodeEditor.TEST_COMPILATION_FAILED = 2;
CMS.CodeEditor.TEST_EVALUATING = 3;
CMS.CodeEditor.TEST_EVALUATED = 4;

// How long we keep polling a user test before giving up (ms).
CMS.CodeEditor.POLL_TIMEOUT = 5 * 60 * 1000;
// Longest program output shown inline (characters).
CMS.CodeEditor.MAX_OUTPUT_SHOWN = 64 * 1024;

CMS.CodeEditor.MODES = {
    ".c": "text/x-csrc",
    ".cpp": "text/x-c++src",
    ".cc": "text/x-c++src",
    ".cxx": "text/x-c++src",
    ".c++": "text/x-c++src",
    ".h": "text/x-c++src",
    ".java": "text/x-java",
    ".cs": "text/x-csharp",
    ".kt": "text/x-kotlin",
    ".scala": "text/x-scala",
    ".py": {name: "python", version: 3},
    ".pas": "text/x-pascal",
    ".pp": "text/x-pascal",
    ".rs": "text/x-rustsrc",
    ".hs": "text/x-haskell",
    ".php": "application/x-httpd-php",
    ".js": "text/javascript",
};


/* Small helpers. */

CMS.CodeEditor.prototype.storage_get = function (key) {
    try {
        return window.localStorage.getItem(this.storage_prefix + key);
    } catch (e) {
        return null;
    }
};

CMS.CodeEditor.prototype.storage_set = function (key, value) {
    try {
        window.localStorage.setItem(this.storage_prefix + key, value);
        return true;
    } catch (e) {
        // Private browsing, quota exceeded, disabled storage...
        return false;
    }
};

CMS.CodeEditor.prototype.format = function (template, params) {
    return template.replace(/%\((\w+)\)s/g, function (match, name) {
        return params[name] !== undefined ? params[name] : match;
    });
};

CMS.CodeEditor.prototype.utf8_length = function (text) {
    return new Blob([text]).size;
};

CMS.CodeEditor.prototype.language = function () {
    var name = $("#editor-language").val();
    for (var i = 0; i < this.cfg.languages.length; i++) {
        if (this.cfg.languages[i].name === name) {
            return this.cfg.languages[i];
        }
    }
    return this.cfg.languages[0];
};

CMS.CodeEditor.prototype.real_filename = function (file) {
    // "sol.%l" -> "sol.cpp"
    return file.replace(/\.%l$/, this.language().extension);
};

CMS.CodeEditor.prototype.mode_for = function (language) {
    return CMS.CodeEditor.MODES[language.extension.toLowerCase()]
        || "text/plain";
};


/* Setup. */

CMS.CodeEditor.prototype.init = function () {
    var self = this;
    var container = document.getElementById("code-editor");

    if (typeof CodeMirror === "undefined") {
        // Should never happen since CodeMirror is served locally, but
        // better a plain textarea than no editor at all.
        this.cm = null;
        this.fallback = $("<textarea class=\"code-editor-fallback\" spellcheck=\"false\"></textarea>");
        $(container).append(this.fallback);
    } else {
        this.cm = CodeMirror(container, {
            lineNumbers: true,
            theme: "eclipse",
            indentUnit: 4,
            tabSize: 4,
            indentWithTabs: false,
            lineWrapping: false,
            matchBrackets: true,
            autoCloseBrackets: true,
            styleActiveLine: true,
            highlightSelectionMatches: {showToken: /\w/, annotateScrollbar: false},
            viewportMargin: 50,
            extraKeys: {
                "Tab": function (cm) {
                    if (cm.somethingSelected()) {
                        cm.indentSelection("add");
                    } else {
                        cm.replaceSelection(
                            Array(cm.getOption("indentUnit") + 1).join(" "), "end");
                    }
                },
                "Shift-Tab": function (cm) { cm.indentSelection("subtract"); },
                "Ctrl-/": "toggleComment",
                "Cmd-/": "toggleComment",
                "Ctrl-S": function () { self.save_all_drafts(true); },
                "Cmd-S": function () { self.save_all_drafts(true); },
                "Ctrl-Enter": function () { self.run(); },
                "Cmd-Enter": function () { self.run(); },
            },
        });
        this.cm.on("cursorActivity", function () { self.schedule_status(); });
    }

    // Restore preferences and drafts.
    var saved_language = this.storage_get("language");
    if (saved_language !== null
            && $("#editor-language option").filter(function () {
                return this.value === saved_language;
            }).length > 0) {
        $("#editor-language").val(saved_language);
    }
    var mode = this.mode_for(this.language());
    var restored = false;
    this.cfg.files.forEach(function (file) {
        var text = self.storage_get("file/" + file);
        if (text === null) {
            text = "";
        } else if (text.length > 0) {
            restored = true;
        }
        if (self.cm !== null) {
            self.docs[file] = CodeMirror.Doc(text, mode);
            self.docs[file].on("change", function () { self.on_change(); });
        } else {
            self.docs[file] = text;
        }
    });
    this.show_file(this.current_file);
    this.set_theme(this.storage_get("theme") || "light");
    this.set_font_size(parseInt(this.storage_get("font_size"), 10) || 14);
    var saved_input = this.storage_get("input");
    if (saved_input !== null) {
        $("#editor-stdin").val(saved_input);
    }
    if (this.cm === null) {
        this.fallback.on("input", function () {
            self.docs[self.current_file] = self.fallback.val();
            self.on_change();
        });
    }

    this.bind_events();
    this.update_status();
    if (restored) {
        this.set_draft_state(this.str.draft_restored);
    }
};

CMS.CodeEditor.prototype.bind_events = function () {
    var self = this;

    $(".editor-file-tab").on("click", function (e) {
        e.preventDefault();
        self.show_file($(this).data("file"));
    });
    $("#editor-language").on("change", function () {
        // Each document remembers its mode, so the others get the new
        // one when they are shown again (see show_file).
        if (self.cm !== null) {
            self.cm.setOption("mode", self.mode_for(self.language()));
        }
        self.storage_set("language", $(this).val());
        self.update_file_labels();
    });
    $("#editor-theme").on("click", function () {
        self.set_theme(self.theme === "dark" ? "light" : "dark");
    });
    $("#editor-font-inc").on("click", function () {
        self.set_font_size(self.font_size + 1);
    });
    $("#editor-font-dec").on("click", function () {
        self.set_font_size(self.font_size - 1);
    });
    $("#editor-load").on("click", function () {
        $("#editor-load-file").val("").trigger("click");
    });
    $("#editor-load-file").on("change", function () {
        self.load_file(this.files[0], function (text) {
            self.set_text(text);
        });
    });
    $("#editor-download").on("click", function () {
        self.download();
    });
    $("#editor-clear").on("click", function () {
        if (window.confirm(self.str.confirm_clear)) {
            self.set_text("");
        }
    });
    $("#editor-form").on("submit", function (e) {
        e.preventDefault();
        self.submit();
    });
    $("#editor-run").on("click", function (e) {
        e.preventDefault();
        self.run();
    });
    $("#editor-stdin").on("input", function () {
        self.schedule_save();
    });
    $("#editor-stdin-load").on("click", function () {
        $("#editor-stdin-file").val("").trigger("click");
    });
    $("#editor-stdin-file").on("change", function () {
        self.load_file(this.files[0], function (text) {
            $("#editor-stdin").val(text);
            self.schedule_save();
        });
    });
    // Do not lose unsaved keystrokes when leaving the page.
    $(window).on("beforeunload pagehide", function () {
        self.save_all_drafts(false);
    });
};

CMS.CodeEditor.prototype.show_file = function (file) {
    this.current_file = file;
    if (this.cm !== null) {
        this.cm.swapDoc(this.docs[file]);
        this.cm.setOption("mode", this.mode_for(this.language()));
        this.cm.focus();
    } else {
        this.fallback.val(this.docs[file]);
    }
    $(".editor-file-tab").each(function () {
        $(this).toggleClass("active", $(this).data("file") === file);
    });
    this.update_file_labels();
    this.update_status();
};

CMS.CodeEditor.prototype.update_file_labels = function () {
    var self = this;
    $(".editor-file-tab").each(function () {
        $(this).text(self.real_filename($(this).data("file")));
    });
    $("#editor-filename").text(this.real_filename(this.current_file));
};

CMS.CodeEditor.prototype.get_text = function (file) {
    file = file || this.current_file;
    return this.cm !== null ? this.docs[file].getValue() : this.docs[file];
};

CMS.CodeEditor.prototype.set_text = function (text) {
    if (this.cm !== null) {
        this.docs[this.current_file].setValue(text);
    } else {
        this.docs[this.current_file] = text;
        this.fallback.val(text);
        this.on_change();
    }
};

CMS.CodeEditor.prototype.set_theme = function (theme) {
    this.theme = theme === "dark" ? "dark" : "light";
    if (this.cm !== null) {
        this.cm.setOption("theme", this.theme === "dark" ? "monokai" : "eclipse");
    }
    $("#editor-workspace").toggleClass("editor-dark", this.theme === "dark");
    $("#editor-theme").text(this.theme === "dark" ? this.str.light : this.str.dark);
    this.storage_set("theme", this.theme);
};

CMS.CodeEditor.prototype.set_font_size = function (size) {
    this.font_size = Math.max(10, Math.min(24, size));
    $("#code-editor").css("font-size", this.font_size + "px");
    if (this.cm !== null) {
        this.cm.refresh();
    }
    this.storage_set("font_size", String(this.font_size));
};

CMS.CodeEditor.prototype.load_file = function (file, callback) {
    if (!file) {
        return;
    }
    var self = this;
    var reader = new FileReader();
    reader.onload = function () { callback(reader.result); };
    reader.onerror = function () { self.show_message("error", self.str.load_failed); };
    reader.readAsText(file);
};

CMS.CodeEditor.prototype.download = function () {
    var blob = new Blob([this.get_text()], {type: "text/plain"});
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = this.real_filename(this.current_file);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
};


/* Drafts and status bar. */

CMS.CodeEditor.prototype.on_change = function () {
    // Validation errors refer to the old code.
    if ($("#editor-message").hasClass("alert-error") && !this.busy) {
        this.hide_message();
    }
    this.set_draft_state(this.str.draft_unsaved);
    this.schedule_save();
    this.schedule_status();
};

CMS.CodeEditor.prototype.schedule_save = function () {
    var self = this;
    clearTimeout(this.save_timer);
    this.save_timer = setTimeout(function () { self.save_all_drafts(false); }, 800);
};

CMS.CodeEditor.prototype.save_all_drafts = function (explicit) {
    clearTimeout(this.save_timer);
    var ok = true;
    for (var file in this.docs) {
        ok = this.storage_set("file/" + file, this.get_text(file)) && ok;
    }
    ok = this.storage_set("input", $("#editor-stdin").val() || "") && ok;
    if (ok) {
        this.set_draft_state(explicit ? this.str.draft_saved_now : this.str.draft_saved);
    } else {
        this.set_draft_state(this.str.draft_not_saved, true);
    }
};

CMS.CodeEditor.prototype.set_draft_state = function (text, is_error) {
    $("#editor-draft-state").text(text).toggleClass("text-error", !!is_error);
};

CMS.CodeEditor.prototype.schedule_status = function () {
    var self = this;
    clearTimeout(this.status_timer);
    this.status_timer = setTimeout(function () { self.update_status(); }, 120);
};

CMS.CodeEditor.prototype.update_status = function () {
    var size = 0;
    for (var file in this.docs) {
        size += this.utf8_length(this.get_text(file));
    }
    var limit = this.cfg.max_submission_length;
    var size_el = $("#editor-size");
    size_el.text(this.format(this.str.size, {size: size, limit: limit}));
    size_el.toggleClass("editor-over-limit", size > limit);
    if (this.cm !== null) {
        var cursor = this.cm.getCursor();
        $("#editor-cursor").text(this.format(this.str.cursor, {
            line: cursor.line + 1, col: cursor.ch + 1}));
    }
};


/* Messages. */

CMS.CodeEditor.prototype.show_message = function (level, text, title) {
    var box = $("#editor-message");
    box.removeClass("alert-error alert-success alert-info")
       .addClass("alert-" + level)
       .empty();
    if (title) {
        box.append($("<strong></strong>").text(title + " "));
    }
    box.append($("<span></span>").text(text)).show();
};

CMS.CodeEditor.prototype.hide_message = function () {
    $("#editor-message").hide();
};

CMS.CodeEditor.prototype.set_busy = function (busy, button, label) {
    this.busy = busy;
    $("#editor-submit, #editor-run").prop("disabled", busy);
    if (button) {
        if (busy) {
            button.data("label", button.text()).text(label);
        } else if (button.data("label")) {
            button.text(button.data("label"));
        }
    }
};


/* Sending code. */

/**
 * Validate the code and return the FormData to send (or null).
 */
CMS.CodeEditor.prototype.build_form = function () {
    var self = this;
    var total = 0;
    var empty = true;
    var form = new FormData();
    form.append("_xsrf", $("#editor-form input[name=_xsrf]").val());
    form.append("language", this.language().name);
    form.append("response_format", "json");
    this.cfg.files.forEach(function (file) {
        var text = self.get_text(file);
        if (text.trim().length > 0) {
            empty = false;
        }
        var blob = new Blob([text], {type: "text/plain"});
        total += blob.size;
        form.append(file, blob, self.real_filename(file));
    });
    if (empty) {
        this.show_message("error", this.str.empty_code);
        return null;
    }
    if (total > this.cfg.max_submission_length) {
        this.show_message("error", this.format(this.str.too_long, {
            size: total, limit: this.cfg.max_submission_length}));
        return null;
    }
    return form;
};

CMS.CodeEditor.prototype.post = function (url, form, on_success, on_done) {
    var self = this;
    $.ajax({
        type: "POST",
        url: url,
        data: form,
        processData: false,
        contentType: false,
        dataType: "json",
        timeout: 60000,
    }).done(function (data) {
        if (data && data.success) {
            on_success(data);
        } else if (data) {
            self.show_message("error", data.text, data.subject);
        } else {
            self.show_message("error", self.str.bad_reply);
        }
    }).fail(function (xhr, status) {
        var reason;
        if (status === "parsererror") {
            // Most likely the login page: the session has expired.
            reason = self.str.bad_reply;
        } else if (status === "timeout") {
            reason = self.str.timeout;
        } else if (xhr.status === 0) {
            reason = self.str.network_error;
        } else if (xhr.status === 403) {
            reason = self.str.forbidden;
        } else {
            reason = self.format(self.str.server_error, {status: xhr.status});
        }
        self.show_message("error", reason);
    }).always(function () {
        on_done();
    });
};

CMS.CodeEditor.prototype.submit = function () {
    if (this.busy) {
        return;
    }
    this.hide_message();
    this.save_all_drafts(false);
    var form = this.build_form();
    if (form === null) {
        return;
    }
    var self = this;
    var button = $("#editor-submit");
    var succeeded = false;
    this.set_busy(true, button, this.str.submitting);
    this.post(this.cfg.submit_url, form, function (data) {
        succeeded = true;
        self.show_message("success", self.str.submitted);
        // The page lists the new submission and the notification.
        window.location.href = data.redirect;
    }, function () {
        if (!succeeded) {
            self.set_busy(false, button);
        }
    });
};

CMS.CodeEditor.prototype.run = function () {
    if (this.busy || !this.cfg.test_url) {
        return;
    }
    this.hide_message();
    this.save_all_drafts(false);
    var form = this.build_form();
    if (form === null) {
        return;
    }
    var input = $("#editor-stdin").val() || "";
    var input_blob = new Blob([input], {type: "text/plain"});
    if (input_blob.size > this.cfg.max_input_length) {
        this.show_message("error", this.format(this.str.input_too_long, {
            size: input_blob.size, limit: this.cfg.max_input_length}));
        return;
    }
    form.append("input", input_blob, "input.txt");

    var self = this;
    var button = $("#editor-run");
    var polling = false;
    // Forget the previous run.
    $("#editor-result-time, #editor-result-memory").text("");
    $("#editor-result-download").hide();
    $("#editor-result-details").hide().prop("open", false)
        .find(".editor-details-body").empty();
    $("#editor-result-output").text("");
    this.set_busy(true, button, this.str.running);
    this.show_result_state("info", this.str.sending);
    this.post(this.cfg.test_url, form, function (data) {
        polling = true;
        self.poll_test(data.user_test_num, Date.now(), 800, function () {
            self.set_busy(false, button);
        });
    }, function () {
        if (!polling) {
            self.set_busy(false, button);
            self.show_result_state("error", self.str.run_failed);
        }
    });
};


/* Following a user test. */

CMS.CodeEditor.prototype.test_url = function (num, what) {
    var url = this.cfg.tests_url + "/" + num;
    return what ? url + "/" + what : url;
};

CMS.CodeEditor.prototype.show_result_state = function (level, text) {
    $("#editor-result").show();
    $("#editor-result-state")
        .removeClass("label-info label-success label-important label-warning")
        .addClass({info: "label-info", success: "label-success",
                   error: "label-important", warning: "label-warning"}[level])
        .text(text);
};

CMS.CodeEditor.prototype.poll_test = function (num, started, delay, done) {
    var self = this;
    clearTimeout(this.poll_timer);
    if (Date.now() - started > CMS.CodeEditor.POLL_TIMEOUT) {
        this.show_result_state("warning", this.str.still_running);
        done();
        return;
    }
    this.poll_timer = setTimeout(function () {
        $.ajax({url: self.test_url(num), dataType: "json", timeout: 15000})
            .done(function (data) {
                self.on_test_status(num, data, started, delay, done);
            })
            .fail(function () {
                // Transient failure (e.g. server restarting): keep trying.
                self.poll_test(num, started, Math.min(delay * 1.5, 5000), done);
            });
    }, delay);
};

CMS.CodeEditor.prototype.on_test_status = function (num, data, started, delay, done) {
    var E = CMS.CodeEditor;
    $("#editor-result-time, #editor-result-memory").text("");
    if (data.status === E.TEST_COMPILING) {
        this.show_result_state("info", this.str.compiling);
    } else if (data.status === E.TEST_EVALUATING) {
        this.show_result_state("info", this.str.executing);
    } else if (data.status === E.TEST_COMPILATION_FAILED) {
        this.show_result_state("error", this.str.compilation_failed);
        $("#editor-result-output").text(this.str.see_details);
        this.load_details(num, true);
        done();
        return;
    } else if (data.status === E.TEST_EVALUATED) {
        this.show_result_state("success", this.str.executed);
        if (data.execution_time) {
            $("#editor-result-time").text(this.format(this.str.time, {time: data.execution_time}));
        }
        if (data.memory) {
            $("#editor-result-memory").text(this.format(this.str.memory, {memory: data.memory}));
        }
        this.load_output(num, data.output);
        this.load_details(num, false);
        done();
        return;
    }
    this.poll_test(num, started, Math.min(delay * 1.3, 3000), done);
};

CMS.CodeEditor.prototype.load_output = function (num, has_output) {
    var self = this;
    var out = $("#editor-result-output");
    if (!has_output) {
        out.text(this.str.no_output);
        return;
    }
    $.ajax({url: this.test_url(num, "output"), dataType: "text", timeout: 30000})
        .done(function (text) {
            var max = CMS.CodeEditor.MAX_OUTPUT_SHOWN;
            if (text.length > max) {
                text = text.substr(0, max) + "\n" + self.str.output_truncated;
            }
            out.text(text.length > 0 ? text : self.str.empty_output);
            $("#editor-result-download")
                .attr("href", self.test_url(num, "output")).show();
        })
        .fail(function () {
            out.text(self.str.output_unavailable);
        });
};

CMS.CodeEditor.prototype.load_details = function (num, open) {
    var details = $("#editor-result-details");
    details.find(".editor-details-body").load(this.test_url(num, "details"));
    details.show().prop("open", !!open);
};


$(document).ready(function () {
    var config_el = document.getElementById("cws-editor-config");
    if (config_el === null) {
        return;
    }
    var editor = null;
    var start = function () {
        if (editor === null) {
            editor = new CMS.CodeEditor(
                JSON.parse(config_el.textContent),
                JSON.parse(document.getElementById("cws-editor-strings").textContent));
            editor.init();
            CMS.editor = editor;
        } else if (editor.cm !== null) {
            editor.cm.refresh();
        }
    };

    var set_mode = function (mode) {
        $(".mode-toggle-btn").each(function () {
            var active = $(this).data("mode") === mode;
            $(this).toggleClass("active", active).attr("aria-selected", active);
        });
        $(".submission-mode-container").hide();
        $(".submission-mode-container[data-mode=\"" + mode + "\"]").show();
        try {
            window.localStorage.setItem("cms-editor/mode", mode);
        } catch (e) { /* not critical */ }
        if (mode === "editor") {
            start();
        }
    };

    $(".mode-toggle-btn").on("click", function (e) {
        e.preventDefault();
        set_mode($(this).data("mode"));
    });

    var saved_mode = null;
    try {
        saved_mode = window.localStorage.getItem("cms-editor/mode");
    } catch (e) { /* not critical */ }
    set_mode(saved_mode === "editor" ? "editor" : "upload");
});
