#!/usr/bin/python3
# Copyright (C) 2019 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Publishing VCS changes."""

from aiohttp import web
import asyncio
import functools
import shlex
import sys
import urllib.parse

from prometheus_client import (
    Counter,
    Gauge,
    push_to_gateway,
    REGISTRY,
)

from breezy.plugins.propose.propose import (
    MergeProposalExists,
    )

from silver_platter.proposal import (
    publish_changes as publish_changes_from_workspace,
    propose_changes,
    push_changes,
    push_derived_changes,
    find_existing_proposed,
    get_hoster,
    hosters,
    NoSuchProject,
    PermissionDenied,
    UnsupportedHoster,
    )
from silver_platter.debian.lintian import (
    create_mp_description,
    parse_mp_description,
    update_proposal_commit_message,
    )
from silver_platter.utils import (
    open_branch,
    BranchMissing,
    BranchUnavailable,
    )

from . import (
    state,
    ADDITIONAL_COLOCATED_BRANCHES,
    )
from .policy import (
    read_policy,
    apply_policy,
    )
from .prometheus import setup_metrics
from .trace import note, warning
from .vcs import LocalVcsManager


JANITOR_BLURB = """
This merge proposal was created automatically by the Janitor bot
(https://janitor.debian.net/%(suite)s).

You can follow up to this merge proposal as you normally would.
"""


OLD_JANITOR_BLURB = """
This merge proposal was created automatically by the Janitor bot
(https://janitor.debian.net/).

You can follow up to this merge proposal as you normally would.
"""


LOG_BLURB = """
Build and test logs for this branch can be found at
https://janitor.debian.net/cupboard/pkg/%(package)s/%(log_id)s/.
"""


MODE_SKIP = 'skip'
MODE_BUILD_ONLY = 'build-only'
MODE_PUSH = 'push'
MODE_PUSH_DERIVED = 'push-derived'
MODE_PROPOSE = 'propose'
MODE_ATTEMPT_PUSH = 'attempt-push'
SUPPORTED_MODES = [
    MODE_PUSH,
    MODE_SKIP,
    MODE_BUILD_ONLY,
    MODE_PUSH_DERIVED,
    MODE_PROPOSE,
    MODE_ATTEMPT_PUSH,
    ]


proposal_rate_limited_count = Counter(
    'proposal_rate_limited',
    'Number of attempts to create a proposal that was rate-limited',
    ['package', 'suite'])
open_proposal_count = Gauge(
    'open_proposal_count', 'Number of open proposals.',
    labelnames=('maintainer',))
merge_proposal_count = Gauge(
    'merge_proposal_count', 'Number of merge proposals by status.',
    labelnames=('status',))
last_success_gauge = Gauge(
    'job_last_success_unixtime',
    'Last time a batch job successfully finished')


def strip_janitor_blurb(text, suite):
    try:
        i = text.index(JANITOR_BLURB % {'suite': suite})
    except ValueError:
        pass
    else:
        return text[:i].strip()

    i = text.index(OLD_JANITOR_BLURB)
    return text[:i].strip()


def add_janitor_blurb(text, pkg, log_id, suite):
    text += '\n' + (JANITOR_BLURB % {'suite': suite})
    text += (LOG_BLURB % {'package': pkg, 'log_id': log_id, 'suite': suite})
    return text


async def iter_all_mps():
    for name, hoster_cls in hosters.items():
        for instance in hoster_cls.iter_instances():
            note('Checking merge proposals on %r...', instance)
            for status in ['open', 'merged', 'closed']:
                for mp in instance.iter_my_proposals(status=status):
                    yield mp, status


class MaintainerRateLimiter(object):

    def __init__(self, max_mps_per_maintainer=None):
        self._max_mps_per_maintainer = max_mps_per_maintainer
        self._open_mps_per_maintainer = None

    def set_open_mps_per_maintainer(self, open_mps_per_maintainer):
        self._open_mps_per_maintainer = open_mps_per_maintainer
        for maintainer_email, count in open_mps_per_maintainer.items():
            open_proposal_count.labels(maintainer=maintainer_email).set(count)

    def allowed(self, maintainer_email):
        if not self._max_mps_per_maintainer:
            return True
        if self._open_mps_per_maintainer is None:
            # Be conservative
            return False
        current = self._open_mps_per_maintainer.get(maintainer_email, 0)
        return (current < self._max_mps_per_maintainer)

    def inc(self, maintainer_email):
        if self._open_mps_per_maintainer is None:
            return
        self._open_mps_per_maintainer.setdefault(maintainer_email, 0)
        self._open_mps_per_maintainer[maintainer_email] += 1
        open_proposal_count.labels(maintainer=maintainer_email).inc()


class NonRateLimiter(object):

    def allowed(self, email):
        return True

    def inc(self, maintainer_email):
        pass


class PublishFailure(Exception):

    def __init__(self, code, description):
        self.code = code
        self.description = description


class BranchWorkspace(object):
    """Workspace-like object that doesn't use working trees.
    """

    def __init__(self, main_branch, local_branch, resume_branch=None):
        self.main_branch = main_branch
        self.local_branch = local_branch
        self.resume_branch = resume_branch
        self.orig_revid = (resume_branch or main_branch).last_revision()
        self.additional_colocated_branches = ADDITIONAL_COLOCATED_BRANCHES

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def changes_since_main(self):
        return self.local_branch.last_revision() \
               != self.main_branch.last_revision()

    def changes_since_resume(self):
        return self.orig_revid != self.local_branch.last_revision()

    def propose(self, name, description, hoster=None, existing_proposal=None,
                overwrite_existing=None, labels=None, dry_run=False,
                commit_message=None):
        if hoster is None:
            hoster = get_hoster(self.main_branch)
        return propose_changes(
            self.local_branch, self.main_branch,
            hoster=hoster, name=name, mp_description=description,
            resume_branch=self.resume_branch,
            resume_proposal=existing_proposal,
            overwrite_existing=overwrite_existing,
            labels=labels, dry_run=dry_run,
            commit_message=commit_message,
            additional_colocated_branches=self.additional_colocated_branches)

    def push(self, hoster=None, dry_run=False):
        if hoster is None:
            hoster = get_hoster(self.main_branch)
        return push_changes(
            self.local_branch, self.main_branch, hoster=hoster,
            additional_colocated_branches=self.additional_colocated_branches,
            dry_run=dry_run)

    def push_derived(self, name, hoster=None, overwrite_existing=False):
        if hoster is None:
            hoster = get_hoster(self.main_branch)
        return push_derived_changes(
            self.local_branch,
            self.main_branch, hoster, name,
            overwrite_existing=overwrite_existing)


async def publish(
        suite, pkg, maintainer_email, subrunner, mode, hoster,
        main_branch, local_branch, resume_branch=None,
        dry_run=False, log_id=None, existing_proposal=None,
        allow_create_proposal=False):
    def get_proposal_description(existing_proposal):
        if existing_proposal:
            existing_description = existing_proposal.get_description()
            existing_description = strip_janitor_blurb(
                existing_description, suite)
        else:
            existing_description = None
        description = subrunner.get_proposal_description(
            existing_description)
        return add_janitor_blurb(description, pkg, log_id, suite)

    def get_proposal_commit_message(existing_proposal):
        if existing_proposal:
            existing_commit_message = (
                getattr(existing_proposal, 'get_commit_message',
                        lambda: None)())
        else:
            existing_commit_message = None
        return subrunner.get_proposal_commit_message(
            existing_commit_message)

    with BranchWorkspace(
            main_branch, local_branch, resume_branch=resume_branch) as ws:
        if not hoster.supports_merge_proposal_labels:
            labels = None
        else:
            labels = [suite]
        try:
            (proposal, is_new) = publish_changes_from_workspace(
                ws, mode, subrunner.branch_name(),
                get_proposal_description=get_proposal_description,
                get_proposal_commit_message=(
                    get_proposal_commit_message),
                dry_run=dry_run, hoster=hoster,
                allow_create_proposal=allow_create_proposal,
                overwrite_existing=True,
                existing_proposal=existing_proposal,
                labels=labels)
        except NoSuchProject as e:
            raise PublishFailure(
                description='project %s was not found' % e.project,
                code='project-not-found')
        except PermissionDenied as e:
            raise PublishFailure(
                description=str(e), code='permission-denied')
        except MergeProposalExists as e:
            raise PublishFailure(
                description=str(e), code='merge-proposal-exists')

        if proposal and is_new:
            merge_proposal_count.labels(status='open').inc()
            open_proposal_count.labels(
                maintainer=maintainer_email).inc()

    return proposal, is_new


class LintianBrushPublisher(object):

    def __init__(self, args):
        self.args = args

    def branch_name(self):
        return "lintian-fixes"

    def get_proposal_description(self, existing_description):
        if existing_description:
            existing_lines = parse_mp_description(existing_description)
        else:
            existing_lines = []
        return create_mp_description(
            existing_lines + [l['summary'] for l in self.applied])

    def get_proposal_commit_message(self, existing_commit_message):
        applied = []
        for result in self.applied:
            applied.append((result['fixed_lintian_tags'], result['summary']))
        return update_proposal_commit_message(existing_commit_message, applied)

    def read_worker_result(self, result):
        self.applied = result['applied']
        self.failed = result['failed']
        self.add_on_only = result['add_on_only']

    def allow_create_proposal(self):
        return self.applied and not self.add_on_only


class NewUpstreamPublisher(object):

    def __init__(self, args):
        self.args = args

    def branch_name(self):
        if '--snapshot' in self.args:
            return "new-upstream-snapshot"
        else:
            return "new-upstream"

    def read_worker_result(self, result):
        self._upstream_version = result['upstream_version']

    def get_proposal_description(self, existing_description):
        return "New upstream version %s.\n" % self._upstream_version

    def get_proposal_commit_message(self, existing_commit_message):
        return self.get_proposal_description(None)

    def allow_create_proposal(self):
        # No upstream release too small...
        return True


async def publish_one(
        suite, pkg, command, subworker_result, main_branch_url,
        mode, log_id, maintainer_email, vcs_manager, branch_name,
        dry_run=False, possible_hosters=None,
        possible_transports=None, allow_create_proposal=None):
    assert mode in SUPPORTED_MODES
    local_branch = vcs_manager.get_branch(pkg, branch_name)
    if local_branch is None:
        raise PublishFailure(
            'result-branch-not-found', 'can not find local branch')

    if command.startswith('new-upstream'):
        subrunner = NewUpstreamPublisher(command)
    elif command == 'lintian-brush':
        subrunner = LintianBrushPublisher(command)
    else:
        raise AssertionError('unknown command %r' % command)

    try:
        main_branch = open_branch(
            main_branch_url, possible_transports=possible_transports)
    except BranchUnavailable as e:
        raise PublishFailure('branch-unavailable', str(e))
    except BranchMissing as e:
        raise PublishFailure('branch-missing', str(e))

    subrunner.read_worker_result(subworker_result)
    branch_name = subrunner.branch_name()

    try:
        hoster = get_hoster(main_branch, possible_hosters=possible_hosters)
    except UnsupportedHoster as e:
        if mode not in (MODE_PUSH, MODE_BUILD_ONLY):
            netloc = urllib.parse.urlparse(main_branch.user_url).netloc
            raise PublishFailure(
                description='Hoster unsupported: %s.' % netloc,
                code='hoster-unsupported')
        # We can't figure out what branch to resume from when there's no hoster
        # that can tell us.
        resume_branch = None
        existing_proposal = None
        if mode == MODE_PUSH:
            warning('Unsupported hoster (%s), will attempt to push to %s',
                    e, main_branch.user_url)
    else:
        try:
            (resume_branch, overwrite, existing_proposal) = (
                find_existing_proposed(
                    main_branch, hoster, branch_name))
        except NoSuchProject as e:
            if mode not in (MODE_PUSH, MODE_BUILD_ONLY):
                raise PublishFailure(
                    description='Project %s not found.' % e.project,
                    code='project-not-found')
            resume_branch = None
            existing_proposal = None

    if allow_create_proposal is None:
        allow_create_proposal = subrunner.allow_create_proposal()
    proposal, is_new = await publish(
        suite, pkg, maintainer_email,
        subrunner, mode, hoster, main_branch, local_branch,
        resume_branch,
        dry_run=dry_run, log_id=log_id,
        existing_proposal=existing_proposal,
        allow_create_proposal=allow_create_proposal)

    return proposal, branch_name, is_new


async def publish_pending_new(rate_limiter, policy, vcs_manager,
                              dry_run=False):
    possible_hosters = []
    possible_transports = []

    async for (pkg, command, build_version, result_code, context,
               start_time, log_id, revision, subworker_result, branch_name,
               suite, maintainer_email, uploader_emails, main_branch_url,
               main_branch_revision) in state.iter_publish_ready():

        mode, unused_update_changelog, unused_committer = apply_policy(
            policy, suite.replace('-', '_'), pkg, maintainer_email,
            uploader_emails or [])
        if mode in (MODE_BUILD_ONLY, MODE_SKIP):
            continue
        if await state.already_published(pkg, branch_name, revision, mode):
            continue
        if not rate_limiter.allowed(maintainer_email) and \
                mode in (MODE_PROPOSE, MODE_ATTEMPT_PUSH):
            proposal_rate_limited_count.labels(package=pkg, suite=suite).inc()
            warning(
                'Not creating proposal for %s, maximum number of open merge '
                'proposals reached for maintainer %s', pkg, maintainer_email)
            if mode == MODE_PROPOSE:
                mode = MODE_BUILD_ONLY
            if mode == MODE_ATTEMPT_PUSH:
                mode = MODE_PUSH
        if mode == MODE_ATTEMPT_PUSH and \
                "salsa.debian.org/debian/" in main_branch_url:
            # Make sure we don't accidentally push to unsuspecting collab-maint
            # repositories, even if debian-janitor becomes a member of "debian"
            # in the future.
            mode = MODE_PROPOSE
        if mode in (MODE_BUILD_ONLY, MODE_SKIP):
            continue
        note('Publishing %s / %r (mode: %s)', pkg, command, mode)
        try:
            proposal, branch_name, is_new = await publish_one(
                suite, pkg, command, subworker_result,
                main_branch_url, mode, log_id, maintainer_email,
                vcs_manager=vcs_manager, branch_name=branch_name,
                dry_run=dry_run, possible_hosters=possible_hosters,
                possible_transports=possible_transports)
        except PublishFailure as e:
            code = e.code
            description = e.description
            branch_name = None
            proposal = None
            note('Failed(%s): %s', code, description)
        else:
            code = 'success'
            description = 'Success'
            if proposal and is_new:
                rate_limiter.inc(maintainer_email)

        await state.store_publish(
            pkg, branch_name, main_branch_revision,
            revision, mode, code, description,
            proposal.url if proposal else None)


async def publish_request(rate_limiter, dry_run, vcs_manager, request):
    package = request.match_info['package']
    suite = request.match_info['suite']
    post = await request.post()
    mode = post.get('mode', MODE_PROPOSE)
    try:
        package = await state.get_package(package)
    except IndexError:
        return web.json_response({}, status=400)

    if (mode in (MODE_PROPOSE, MODE_ATTEMPT_PUSH) and
            not rate_limiter.allowed(package.maintainer_email)):
        return web.json_response(
            {'maintainer_email': package.maintainer_email,
             'code': 'rate-limited',
             'description':
                'Maximum number of open merge proposals for maintainer '
                'reached'},
            status=429)

    if mode in (MODE_SKIP, MODE_BUILD_ONLY):
        return web.json_response(
            {'code': 'done',
             'description':
                'Nothing to do'})

    run = await state.get_last_unmerged_success(package.name, suite)
    if run is None:
        return web.json_response({}, status=400)
    note('Handling request to publish %s/%s', package.name, suite)
    try:
        proposal, branch_name, is_new = await publish_one(
            suite, package.name, run.command, run.result,
            run.branch_url, mode, run.id, package.maintainer_email,
            vcs_manager=vcs_manager, branch_name=run.branch_name,
            dry_run=dry_run, allow_create_proposal=True)
    except PublishFailure as e:
        return web.json_response(
            {'code': e.code, 'description': e.description}, status=400)

    if proposal and is_new:
        rate_limiter.inc(package.maintainer_email)

    return web.json_response(
        {'branch_name': branch_name,
         'mode': mode,
         'is_new': is_new,
         'proposal': proposal.url if proposal else None}, status=200)


async def run_web_server(listen_addr, port, rate_limiter, vcs_manager,
                         dry_run=False):
    app = web.Application()
    setup_metrics(app)
    app.router.add_post(
        "/{suite}/{package}/publish",
        functools.partial(publish_request, rate_limiter, dry_run, vcs_manager))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen_addr, port)
    await site.start()


async def process_queue_loop(rate_limiter, policy, dry_run, vcs_manager,
                             interval, auto_publish=True):
    while True:
        await check_existing(rate_limiter, vcs_manager, dry_run)
        await asyncio.sleep(interval)
        if auto_publish:
            await publish_pending_new(rate_limiter, policy, vcs_manager, dry_run)


def is_conflicted(mp):
    try:
        return not mp.can_be_merged()
    except NotImplementedError:
        # TODO(jelmer): Download and attempt to merge locally?
        return None


async def check_existing(rate_limiter, vcs_manager, dry_run=False):
    open_mps_per_maintainer = {}
    status_count = {'open': 0, 'closed': 0, 'merged': 0}
    async for mp, status in iter_all_mps():
        await state.set_proposal_status(mp.url, status)
        status_count[status] += 1
        if status != 'open':
            continue
        maintainer_email = await state.get_maintainer_email_for_proposal(
            mp.url)
        if maintainer_email is None:
            source_branch_url = mp.get_source_branch_url()
            maintainer_email = await state.get_maintainer_email_for_branch_url(
                source_branch_url)
            if maintainer_email is None:
                warning('No maintainer email known for %s', mp.url)
        if maintainer_email is not None:
            open_mps_per_maintainer.setdefault(maintainer_email, 0)
            open_mps_per_maintainer[maintainer_email] += 1
        mp_run = await state.get_merge_proposal_run(mp.url)
        if mp_run is None:
            warning('Unable to find local metadata for %s, skipping.', mp.url)
            continue

        recent_runs = []
        async for run in state.iter_previous_runs(
                mp_run.package, mp_run.suite):
            if run == mp_run:
                break
            recent_runs.append(run)

        for run in recent_runs:
            if run.result_code not in ('success', 'nothing-to-do'):
                note('%s: Last run failed (%s). Not touching merge proposal.',
                     mp.url, run.result_code)
                break

            if run.result_code == 'nothing-to-do':
                continue

            if run.suite == 'unchanged':
                continue

            note('%s needs to be updated.', mp.url)
            try:
                mp, branch_name, is_new = await publish_one(
                    run.suite, run.package, run.command, run.result,
                    run.branch_url, MODE_PROPOSE, run.id,
                    maintainer_email,
                    vcs_manager=vcs_manager, branch_name=run.branch_name,
                    dry_run=dry_run, allow_create_proposal=True)
            except PublishFailure as e:
                note('%s: Updating merge proposal failed: %s (%s)',
                     mp.url, e.code, e.description)
                await state.store_publish(
                    run.package, branch_name, run.main_branch_revision,
                    run.revision, MODE_PROPOSE, e.code, e.description,
                    mp.url)
                break
            else:
                await state.store_publish(
                    run.package, branch_name,
                    run.main_branch_revision.decode('utf-8'),
                    run.revision.decode('utf-8'), MODE_PROPOSE, 'success',
                    'Succesfully updated', mp.url)

                assert not is_new, "Intended to update proposal %r" % mp
                break
        else:
            if recent_runs:
                # A new run happened since the last, but there was nothing to
                # do.
                if False:
                    note('%s: Last run did not produce any changes, '
                         'closing proposal.', mp.url)
                    mp.close()
                    continue

            # It may take a while for the 'conflicted' bit on the proposal to
            # be refreshed, so only check it if we haven't made any other
            # changes.
            if is_conflicted(mp):
                note('%s is conflicted. Rescheduling.', mp.url)
                await state.add_to_queue(
                    run.branch_url, run.package, shlex.split(run.command),
                    run.suite, offset=-2, refresh=True)

    for status, count in status_count.items():
        merge_proposal_count.labels(status=status).set(count)

    rate_limiter.set_open_mps_per_maintainer(open_mps_per_maintainer)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='janitor.publish')
    parser.add_argument(
        '--max-mps-per-maintainer',
        default=0,
        type=int,
        help='Maximum number of open merge proposals per maintainer.')
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true", default=False)
    parser.add_argument(
        '--vcs-result-dir', type=str,
        help='Directory to store VCS repositories in.',
        default='vcs')
    parser.add_argument(
        "--policy",
        help="Policy file to read.", type=str,
        default='policy.conf')
    parser.add_argument(
        '--prometheus', type=str,
        help='Prometheus push gateway to export to.')
    parser.add_argument(
        '--once', action='store_true',
        help="Just do one pass over the queue, don't run as a daemon.")
    parser.add_argument(
        '--listen-address', type=str,
        help='Listen address', default='localhost')
    parser.add_argument(
        '--port', type=int,
        help='Listen port', default=9912)
    parser.add_argument(
        '--interval', type=int,
        help=('Seconds to wait in between publishing '
              'pending proposals'), default=7200)
    parser.add_argument(
        '--no-auto-publish',
        action='store_true',
        help='Do not create merge proposals automatically.')

    args = parser.parse_args()

    with open(args.policy, 'r') as f:
        policy = read_policy(f)

    if args.max_mps_per_maintainer > 0:
        rate_limiter = MaintainerRateLimiter(args.max_mps_per_maintainer)
    else:
        rate_limiter = NonRateLimiter()

    if args.no_auto_publish and args.once:
        sys.stderr.write('--no-auto-publish and --once are mutually exclude.')
        sys.exit(1)

    loop = asyncio.get_event_loop()
    vcs_manager = LocalVcsManager(args.vcs_result_dir)
    if args.once:
        loop.run_until_complete(publish_pending_new(
            policy, dry_run=args.dry_run,
            vcs_manager=vcs_manager))

        last_success_gauge.set_to_current_time()
        if args.prometheus:
            push_to_gateway(
                args.prometheus, job='janitor.publish',
                registry=REGISTRY)
    else:
        loop.run_until_complete(asyncio.gather(
            loop.create_task(process_queue_loop(
                rate_limiter, policy, dry_run=args.dry_run,
                vcs_manager=vcs_manager, interval=args.interval,
                auto_publish=not args.no_auto_publish)),
            loop.create_task(
                run_web_server(
                    args.listen_address, args.port, rate_limiter,
                    vcs_manager, args.dry_run))))


if __name__ == '__main__':
    sys.exit(main(sys.argv))
