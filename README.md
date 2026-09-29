Contest Management System
=========================

Homepage: <http://cms-dev.github.io/>

This repository is a fork of <https://github.com/cms-dev/cms>.

Custom changes in this fork
---------------------------

- Contest web server UI redesign for the contest listing, task description, submissions, and login pages.
- Built-in contest submission code editor with language selection and file/code toggle.
- Admin web server CSV user upload flow with generated usernames and secure random passwords.
- Ranking web server updates for multi-contest mode with a contest selector dropdown.
- Deployment-specific fixes for cache busting and service binding in this environment.
- Code editor v2: CodeMirror served locally (works offline), all contest
  languages, multi-file tasks, drafts saved in the browser, and "Run" on a
  custom input (through user tests) with inline output and compiler errors.
  Submissions are sent via XHR with inline error reporting.
- ProxyService stability: data is re-queued instead of lost while the ranking
  is down, request timeouts, no crash at startup if the ranking is down, and
  users added mid-contest reach the ranking.
- Watchdog rewrite (`scripts/cms-watchdog.py`): hung-process detection,
  exponential restart backoff, config-driven health checks, and a `status`
  command; systemd unit sample and `cms-watchdog-util` included.
- `man.txt`: a manual for every CMS command, the watchdog, and the editor.

[![Build Status](https://github.com/cms-dev/cms/actions/workflows/main.yml/badge.svg)](https://github.com/cms-dev/cms/actions)
[![Codecov](https://codecov.io/gh/cms-dev/cms/branch/master/graph/badge.svg)](https://codecov.io/gh/cms-dev/cms)
[![Get support on Telegram](https://img.shields.io/badge/Questions%3F-Join%20the%20Telegram%20group!-%2326A5E4?style=flat&logo=telegram)](https://t.me/contestms)

[🌍 Help translate CMS in your language!](https://cms-dev.oneskyapp.com/collaboration/project?id=392655)

Introduction
------------

CMS, or Contest Management System, is a distributed system for running
and (to some extent) organizing a programming contest.

CMS has been designed to be general and to handle many different types
of contests, tasks, scorings, etc. Nonetheless, CMS has been
explicitly build to be used in the 2012 International Olympiad in
Informatics, held in September 2012 in Italy.


Download
--------

**For end-users it's best to download the latest stable version of CMS,
which can be found already packaged at <http://cms-dev.github.io/>.**

This git repository, which contains the development version in its
master branch, is intended for developers and everyone interested in
contributing or just curious to see how the code works and wanting to
hack on it.

Please note that since the sandbox is contained in a
[git submodule](http://git-scm.com/docs/git-submodule) you should append
`--recursive` to the standard `git clone` command to obtain it. Or, if
you have already cloned CMS, simply run the following command from
inside the repository:

```bash
git submodule update --init
```


Support
-------

To learn how to install and use CMS, please read the **documentation**,
available at <https://cms.readthedocs.org/>.

If you have questions or need help troubleshooting some problem, contact us in
the **chat** on [Telegram](https://t.me/contestms), or write on the **support
mailing list** <contestms-support@googlegroups.com>, where no registration is
required (you can see the archives on [Google
Groups](https://groups.google.com/forum/#!forum/contestms-support)).

To help with the troubleshooting, you can upload on some online pastebin the
relevant **log files**, that you can find in `/var/local/log/cms/`.

If you encountered a bug, please file an
[issue](https://github.com/cms-dev/cms/issues) on **GitHub** following the
instructions in the issue template.

**Please don't file issues to ask for help**, we are happy to help on the
mailing list or on Telegram, and it is more likely somebody will answer your
query sooner.

You can subscribe to <contestms-announce@googlegroups.com> to receive
**announcements** of new releases and other important news. Register on
[Google Groups](https://groups.google.com/forum/#!forum/contestms-announce).

For **development** queries, you can write to
<contestms-discuss@googlegroups.com> and as before subscribe or see the
archives on
[Google Groups](https://groups.google.com/forum/#!forum/contestms-discuss).



Testimonials
------------

CMS has been used in several official and unofficial contests. Please
find an updated list at <http://cms-dev.github.io/testimonials.html>.

If you used CMS for a contest, selection, or a similar event, and want
to publicize this information, we would be more than happy to hear
from you and add it to that list.
