#!/usr/bin/python3
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
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

from contextlib import contextmanager, ExitStack
from datetime import datetime
import errno
from functools import partial
from http.client import IncompleteRead
from io import BytesIO
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import traceback
from typing import Any, Optional, List, Dict, Type, Iterator, Tuple
from urllib.parse import urljoin

import aiohttp
from aiohttp import ClientSession, MultipartWriter, BasicAuth, ClientTimeout, ClientResponseError, ClientConnectorError, web
import yarl

from jinja2 import Template

from prometheus_client import REGISTRY, push_to_gateway

import argparse
import asyncio

from silver_platter.workspace import Workspace

from silver_platter.apply import (
    script_runner as generic_script_runner,
    DetailedFailure as GenericDetailedFailure,
    ScriptFailed,
    ScriptMadeNoChanges,
    ResultFileFormatError,
    )
from silver_platter.debian.apply import (
    script_runner as debian_script_runner,
    DetailedFailure as DebianDetailedFailure,
    MissingChangelog,
    )
from silver_platter.debian import (
    MissingUpstreamTarball,
    pick_additional_colocated_branches,
)
from silver_platter.debian.changer import (
    DebianChanger,
    ChangerError,
    ChangerResult,
    ChangerReporter,
    changer_subcommand as _debian_changer_subcommand,
)
from silver_platter.debian.debianize import (
    DebianizeChanger as ActualDebianizeChanger,
)

from silver_platter.debian.upstream import (
    NewUpstreamChanger as ActualNewUpstreamChanger,
)
from silver_platter.proposal import Hoster

from silver_platter.utils import (
    full_branch_url,
    open_branch,
    BranchMissing,
    BranchUnavailable,
)

from ognibuild.debian.fix_build import build_incrementally
from ognibuild.debian.build import (
    build_once,
    MissingChangesFile,
    DetailedDebianBuildFailure,
    UnidentifiedDebianBuildError,
)
from ognibuild.buildsystem import (
    NoBuildToolsFound,
    detect_buildsystems,
)
from ognibuild import (
    UnidentifiedError,
)
from ognibuild.dist import (
    create_dist_schroot,
    DistNoTarball,
)

from breezy import urlutils
from breezy.branch import Branch
from breezy.config import (
    credential_store_registry,
    GlobalStack,
    PlainTextCredentialStore,
)
from breezy.errors import (
    ConnectionError,
    NotBranchError,
    InvalidHttpResponse,
    UnexpectedHttpStatus,
)
from breezy.git.remote import RemoteGitError
from breezy.controldir import ControlDir
from breezy.transform import MalformedTransform
from breezy.transport import Transport

from silver_platter.proposal import enable_tag_pushing

from .compat import shlex_join
from .debian import tree_set_changelog_version
from ognibuild import (
    DetailedFailure,
)
from .prometheus import setup_metrics
from .vcs import (
    LocalVcsManager,
    RemoteVcsManager,
    MirrorFailure,
    import_branches,
    BranchOpenFailure,
    open_branch_ext,
)


DEFAULT_UPLOAD_TIMEOUT = ClientTimeout(30 * 60)


class ResultUploadFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


# Whether to trust packages enough to run code from them,
# e.g. when guessing repo location.
TRUST_PACKAGE = False

MAX_BUILD_ITERATIONS = 50


logger = logging.getLogger(__name__)


@contextmanager
def redirect_output(to_file):
    sys.stdout.flush()
    sys.stderr.flush()
    old_stdout = os.dup(sys.stdout.fileno())
    old_stderr = os.dup(sys.stderr.fileno())
    os.dup2(to_file.fileno(), sys.stdout.fileno())  # type: ignore
    os.dup2(to_file.fileno(), sys.stderr.fileno())  # type: ignore
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(old_stdout, sys.stdout.fileno())
        os.dup2(old_stderr, sys.stderr.fileno())


class NewUpstreamChanger(ActualNewUpstreamChanger):

    def create_dist(self, tree, package, version, target_dir):
        from silver_platter.debian.upstream import DistCommandFailed
        from ognibuild.session import SessionSetupFailure

        os.environ['SETUPTOOLS_SCM_PRETEND_VERSION'] = version

        with open(os.path.join(self.log_directory, 'dist.log'), 'wb') as distf, redirect_output(distf):
            try:
                return create_dist_schroot(
                    tree,
                    subdir=package,
                    target_dir=target_dir,
                    packaging_tree=self.packaging_tree,
                    packaging_subpath=self.packaging_debian_path,
                    chroot=self.schroot,
                )
            except NotImplementedError:
                return None
            except SessionSetupFailure as e:
                raise WorkerFailure('session-setup-failure', str(e))
            except NoBuildToolsFound:
                logger.info("No build tools found, falling back to simple export.")
                return None
            except DetailedFailure as e:
                if e.error.is_global:
                    error_code = e.error.kind
                else:
                    error_code = "dist-" + e.error.kind
                error_description = str(e.error)
                raise ChangerError(
                    summary=error_description, category=error_code, original=e
                )
            except DistNoTarball as e:
                raise ChangerError('dist-no-tarball', str(e))
            except UnidentifiedError as e:
                lines = [line for line in e.lines if line]
                if e.secondary:
                    raise DistCommandFailed(e.secondary.line)
                elif len(lines) == 1:
                    raise DistCommandFailed(lines[0])
                else:
                    raise DistCommandFailed(
                        "%r failed with unidentified error "
                        "(return code %d)" % (e.argv, e.retcode)
                    )

    def make_changes(self, local_tree, subpath, *args, **kwargs):
        self.packaging_tree = local_tree
        self.packaging_debian_path = os.path.join(subpath, 'debian')
        return super(NewUpstreamChanger, self).make_changes(local_tree, subpath, *args, **kwargs)


class DebianizeChanger(ActualDebianizeChanger):

    def create_dist(self, tree, package, version, target_dir):
        from silver_platter.debian.upstream import DistCommandFailed

        os.environ['SETUPTOOLS_SCM_PRETEND_VERSION'] = version

        with open(os.path.join(self.log_directory, 'dist.log'), 'wb') as distf, redirect_output(distf):
            try:
                return create_dist_schroot(
                    tree,
                    subdir=package,
                    target_dir=target_dir,
                    chroot=self.schroot,
                )
            except NotImplementedError:
                return None
            except NoBuildToolsFound:
                logger.info("No build tools found, falling back to simple export.")
                return None
            except DetailedFailure as e:
                if e.error.is_global:
                    error_code = e.error.kind
                else:
                    error_code = "dist-" + e.error.kind
                error_description = str(e.error)
                raise ChangerError(
                    summary=error_description, category=error_code, original=e
                )
            except DistNoTarball as e:
                raise ChangerError('dist-no-tarball', str(e))
            except UnidentifiedError as e:
                lines = [line for line in e.lines if line]
                if e.secondary:
                    raise DistCommandFailed(e.secondary.line)
                elif len(lines) == 1:
                    raise DistCommandFailed(lines[0])
                else:
                    raise DistCommandFailed(
                        "%r failed with unidentified error "
                        "(return code %d)" % (e.argv, e.retcode)
                    )


class WorkerResult(object):
    def __init__(
        self,
        description: Optional[str],
        value: Optional[int],
        branches: Optional[List[Tuple[str, str, bytes, bytes]]],
        tags: Optional[Dict[str, bytes]],
        target: str,
        target_details: Optional[Any],
        subworker: Any,
    ) -> None:
        self.description = description
        self.value = value
        self.branches = branches
        self.tags = tags
        self.target = target
        self.target_details = target_details
        self.subworker = subworker

    def json(self):
        return {
            "value": self.value,
            "subworker": self.subworker,
            "description": self.description,
            "branches": [
                (f, n, br.decode("utf-8") if br else None, r.decode("utf-8"))
                for (f, n, br, r) in self.branches
            ],
            "tags": [(f, n, r.decode("utf-8")) for (f, n, r) in self.tags],
            "target": {
                "name": self.target,
                "details": self.target_details,
            },
        }


class WorkerFailure(Exception):
    """Worker processing failed."""

    def __init__(self, code: str, description: str, details: Optional[Any] = None, followup_actions: Optional[List[Any]] = None) -> None:
        self.code = code
        self.description = description
        self.details = details
        self.followup_actions = followup_actions

    def json(self):
        ret = {
            "code": self.code,
            "description": self.description,
            'details': self.details,
            }
        if self.followup_actions:
            ret['followup_actions'] = [[action.json() for action in scenario] for scenario in self.followup_actions]
        return ret


CUSTOM_DEBIAN_SUBCOMMANDS = {
    "new-upstream": NewUpstreamChanger,
    "debianize": DebianizeChanger,
}


# TODO(jelmer): Just invoke the silver-platter subcommand
def debian_changer_subcommand(n):
    try:
        return CUSTOM_DEBIAN_SUBCOMMANDS[n]
    except KeyError:
        return _debian_changer_subcommand(n)


class WorkerReporter(ChangerReporter):
    def __init__(self, metadata_subworker, resume_result, provide_context, remotes):
        self.metadata_subworker = metadata_subworker
        self.resume_result = resume_result
        self.report_context = provide_context
        self.remotes = remotes

    def report_remote(self, name, url):
        self.remotes[name] = {"url": url}

    def report_metadata(self, key, value):
        self.metadata_subworker[key] = value

    def get_base_metadata(self, key, default_value=None):
        if not self.resume_result:
            return default_value
        return self.resume_result.get(key, default_value)


class Target(object):
    """A build target."""

    name: str

    def parse_args(self, argv):
        raise NotImplementedError(self.parse_args)

    def build(self, ws, subpath, output_directory, env):
        raise NotImplementedError(self.build)

    def additional_colocated_branches(self, main_branch):
        return []

    def directory_name(self) -> str:
        raise NotImplementedError(self.directory_name)

    def make_changes(self, local_tree, subpath, reporter, log_directory, committer=None):
        raise NotImplementedError(self.make_changes)


class DebianScriptChanger(object):

    def __init__(self, args):
        self.args = args

    def make_changes(
        self,
        local_tree,
        subpath,
        update_changelog,
        reporter,
        committer,
        base_proposal=None,
    ):
        script = shlex_join(self.args)
        try:
            command_result = debian_script_runner(
                local_tree, script=script, commit_pending=None,
                resume_metadata=reporter.resume_result, subpath=subpath,
                update_changelog=update_changelog)
        except ResultFileFormatError as e:
            raise WorkerFailure(
                'result-file-format', 'Result file was invalid: %s' % e)
        except ScriptMadeNoChanges:
            raise WorkerFailure('nothing-to-do', 'No changes made')
        except MissingChangelog as e:
            raise WorkerFailure(
                'missing-changelog', 'No changelog present: %s' % e.args[0])
        except DebianDetailedFailure as e:
            raise WorkerFailure(e.result_code, e.description, e.details)
        except ScriptFailed as e:
            raise WorkerFailure(
                'command-failed',
                'Script %s failed to run with code %s' % e.args)
        return ChangerResult(
            description=command_result.description,
            mutator=command_result.context,
            branches=[
                ('main', local_tree.branch.name, command_result.old_revision,
                 command_result.new_revision)],
            tags=dict(command_result.tags) if command_result.tags else None,
            value=command_result.value)


class DebianTarget(Target):
    """Debian target."""

    name = "debian"

    DEFAULT_BUILD_COMMAND = 'sbuild -A -s -v'

    def __init__(self, env):
        self.build_distribution = env.get("BUILD_DISTRIBUTION")
        self.build_command = env.get("BUILD_COMMAND") or self.DEFAULT_BUILD_COMMAND
        self.build_suffix = env.get("BUILD_SUFFIX")
        self.last_build_version = env.get("LAST_BUILD_VERSION")
        self.package = env["PACKAGE"]
        self.chroot = env.get("CHROOT")
        self.lintian_profile = env.get("LINTIAN_PROFILE")
        self.lintian_suppress_tags = env.get("LINTIAN_SUPPRESS_TAGS")
        self.committer = env.get("COMMITTER")
        uc = env.get("DEB_UPDATE_CHANGELOG", "auto")
        if uc == "auto":
            self.update_changelog = None
        elif uc == "update":
            self.update_changelog = True
        elif uc == "leave":
            self.update_changelog = True
        else:
            logging.warning(
                'Invalid value for DEB_UPDATE_CHANGELOG: %s, '
                'defaulting to auto.', uc)
            self.update_changelog = None

    def parse_args(self, argv):
        logging.info('Running %r', argv)
        changer_cls: Type[DebianChanger]
        try:
            changer_cls = debian_changer_subcommand(argv[0])
        except KeyError:
            self.changer = DebianScriptChanger(argv)
        else:
            subparser = argparse.ArgumentParser(prog=changer_cls.name)
            subparser.add_argument(
                "--no-update-changelog",
                action="store_false",
                default=None,
                dest="update_changelog",
                help="do not update the changelog",
            )
            subparser.add_argument(
                "--update-changelog",
                action="store_true",
                dest="update_changelog",
                help="force updating of the changelog",
                default=None,
            )
            subparser.add_argument(
                '--dry-run',
                action='store_true',
                help='Dry run.')
            changer_cls.setup_parser(subparser)
            changer_args = subparser.parse_args(argv[1:])
            if changer_args.update_changelog is not None:
                self.update_changelog = changer_args.update_changelog
            self.changer = changer_cls.from_args(changer_args)

    def make_changes(self, local_tree, subpath, reporter, log_directory, committer=None):
        self.changer.log_directory = log_directory
        try:
            return self.changer.make_changes(
                local_tree,
                subpath=subpath,
                committer=committer,
                update_changelog=self.update_changelog,
                reporter=reporter,
            )
        except ChangerError as e:
            raise WorkerFailure(e.category, e.summary, details=e.details)
        except MemoryError as e:
            raise WorkerFailure('memory-error', str(e))

    def additional_colocated_branches(self, main_branch):
        return pick_additional_colocated_branches(main_branch)

    def build(self, ws, subpath, output_directory, env):
        from ognibuild.debian.apt import AptManager
        from ognibuild.session import SessionSetupFailure
        from ognibuild.session.plain import PlainSession
        from ognibuild.session.schroot import SchrootSession

        if not ws.local_tree.has_filename(os.path.join(subpath, 'debian/changelog')):
            raise WorkerFailure("not-debian-package", "Not a Debian package")

        if self.chroot:
            session = SchrootSession(self.chroot)
        else:
            session = PlainSession()
        try:
            with session:
                apt = AptManager(session)
                if self.build_command:
                    if self.last_build_version:
                        # Update the changelog entry with the previous build version;
                        # This allows us to upload incremented versions for subsequent
                        # runs.
                        tree_set_changelog_version(
                            ws.local_tree, self.last_build_version, subpath
                        )

                    source_date_epoch = ws.local_tree.branch.repository.get_revision(
                        ws.main_branch.last_revision()
                    ).timestamp
                    try:
                        if not self.build_suffix:
                            (changes_names, cl_entry) = build_once(
                                ws.local_tree,
                                self.build_distribution,
                                output_directory,
                                self.build_command,
                                subpath=subpath,
                                source_date_epoch=source_date_epoch,
                            )
                        else:
                            (changes_names, cl_entry) = build_incrementally(
                                ws.local_tree,
                                apt,
                                "~" + self.build_suffix,
                                self.build_distribution,
                                output_directory,
                                build_command=self.build_command,
                                build_changelog_entry="Build for debian-janitor apt repository.",
                                committer=self.committer,
                                subpath=subpath,
                                source_date_epoch=source_date_epoch,
                                update_changelog=self.update_changelog,
                                max_iterations=MAX_BUILD_ITERATIONS
                            )
                    except MissingUpstreamTarball:
                        raise WorkerFailure(
                            "build-missing-upstream-source", "unable to find upstream source"
                        )
                    except MissingChangesFile as e:
                        raise WorkerFailure(
                            "build-missing-changes",
                            "Expected changes path %s does not exist." % e.filename,
                            details={'filename': e.filename}
                        )
                    except DetailedDebianBuildFailure as e:
                        if e.stage and not e.error.is_global:
                            code = "%s-%s" % (e.stage, e.error.kind)
                        else:
                            code = e.error.kind
                        try:
                            details = e.error.json()
                        except NotImplementedError:
                            details = None
                            actions = None
                        else:
                            from .debian.missing_deps import resolve_requirement
                            from ognibuild.buildlog import problem_to_upstream_requirement
                            # Maybe there's a follow-up action we can consider?
                            req = problem_to_upstream_requirement(e.error)
                            if req:
                                actions = resolve_requirement(apt, req)
                                if actions:
                                    logging.info('Suggesting follow-up actions: %r', actions)
                            else:
                                actions = None
                        raise WorkerFailure(code, e.description, details=details, followup_actions=actions)
                    except UnidentifiedDebianBuildError as e:
                        if e.stage is not None:
                            code = "build-failed-stage-%s" % e.stage
                        else:
                            code = "build-failed"
                        raise WorkerFailure(code, e.description)
                    logger.info("Built %r.", changes_names)
        except SessionSetupFailure as e:
            raise WorkerFailure('session-setup-failure', str(e))
        from .debian.lintian import run_lintian
        lintian_result = run_lintian(
            output_directory, changes_names, profile=self.lintian_profile,
            suppress_tags=self.lintian_suppress_tags)
        return {'lintian': lintian_result}

    def directory_name(self):
        return self.package


class GenericTarget(Target):
    """Generic build target."""

    name = "generic"

    def __init__(self, env):
        self.chroot = env.get("CHROOT")

    def parse_args(self, argv):
        self.argv = argv

    def make_changes(self, local_tree, subpath, reporter, log_directory, committer=None):
        script = shlex_join(self.argv)
        try:
            command_result = generic_script_runner(
                local_tree, script=script, commit_pending=None,
                resume_metadata=reporter.resume_result, subpath=subpath)
        except ResultFileFormatError as e:
            raise WorkerFailure(
                'result-file-format', 'Result file was invalid: %s' % e)
        except ScriptMadeNoChanges:
            raise WorkerFailure('nothing-to-do', 'No changes made')
        except GenericDetailedFailure as e:
            raise WorkerFailure(e.result_code, e.description, e.details)
        except ScriptFailed as e:
            raise WorkerFailure(
                'command-failed',
                'Script %s failed to run with code %s' % e.args)
        return ChangerResult(
            description=command_result.description,
            mutator=command_result.context,
            branches=[
                ('main', local_tree.branch.name, command_result.old_revision,
                 command_result.new_revision)],
            tags=dict(command_result.tags) if command_result.tags else None,
            value=command_result.value)

    def additional_colocated_branches(self, main_branch):
        return []

    def build(self, ws, subpath, output_directory, env):
        from ognibuild.build import run_build
        from ognibuild.test import run_test
        from ognibuild.buildlog import InstallFixer
        from ognibuild.session.plain import PlainSession
        from ognibuild.session.schroot import SchrootSession
        from ognibuild.resolver import auto_resolver

        if self.chroot:
            session = SchrootSession(self.chroot)
            logger.info('Using schroot %s', self.chroot)
        else:
            session = PlainSession()
        with session:
            resolver = auto_resolver(session)
            fixers = [InstallFixer(resolver)]
            external_dir, internal_dir = session.setup_from_vcs(ws.local_tree)
            bss = list(detect_buildsystems(os.path.join(external_dir, subpath)))
            session.chdir(os.path.join(internal_dir, subpath))
            try:
                run_build(session, buildsystems=bss, resolver=resolver, fixers=fixers)
                run_test(session, buildsystems=bss, resolver=resolver, fixers=fixers)
            except NoBuildToolsFound as e:
                raise WorkerFailure('no-build-tools-found', str(e))
            except DetailedFailure as f:
                raise WorkerFailure(f.error.kind, str(f.error), details={'command': f.argv})
            except UnidentifiedError as e:
                lines = [line for line in e.lines if line]
                if e.secondary:
                    raise WorkerFailure('build-failed', e.secondary.line)
                elif len(lines) == 1:
                    raise WorkerFailure('build-failed', lines[0])
                else:
                    raise WorkerFailure(
                        'build-failed',
                        "%r failed with unidentified error "
                        "(return code %d)" % (e.argv, e.retcode)
                    )

        return {}

    def directory_name(self):
        return "package"


def _drop_env(command):
    ret = list(command)
    while ret and '=' in ret[0]:
        ret.pop(0)
    return ret


@contextmanager
def process_package(
    vcs_url: str,
    subpath: str,
    env: Dict[str, str],
    command: List[str],
    output_directory: str,
    target: str,
    metadata: Any,
    build_command: Optional[str] = None,
    possible_transports: Optional[List[Transport]] = None,
    possible_hosters: Optional[List[Hoster]] = None,
    resume_branch_url: Optional[str] = None,
    cached_branch_url: Optional[str] = None,
    extra_resume_branches: Optional[List[Tuple[str, str]]] = None,
    resume_subworker_result: Any = None,
    force_build: bool = False
) -> Iterator[Tuple[Workspace, WorkerResult]]:
    committer = env.get("COMMITTER")

    metadata["command"] = command

    build_target: Target
    if target == "debian":
        build_target = DebianTarget(env)
    elif target == "generic":
        build_target = GenericTarget(env)
    else:
        raise WorkerFailure(
            'target-unsupported', 'The target %r is not supported' % target)

    build_target.parse_args(command)

    logger.info("Opening branch at %s", vcs_url)
    try:
        main_branch = open_branch_ext(vcs_url, possible_transports=possible_transports)
    except BranchOpenFailure as e:
        raise WorkerFailure(e.code, e.description, details={'url': vcs_url})

    if cached_branch_url:
        try:
            cached_branch = open_branch(
                cached_branch_url, possible_transports=possible_transports
            )
        except BranchMissing as e:
            logger.info("Cached branch URL %s missing: %s", cached_branch_url, e)
            cached_branch = None
        except BranchUnavailable as e:
            logger.warning(
                "Cached branch URL %s unavailable: %s", cached_branch_url, e
            )
            cached_branch = None
        else:
            logger.info("Using cached branch %s", full_branch_url(cached_branch))
    else:
        cached_branch = None

    if resume_branch_url:
        try:
            resume_branch = open_branch(
                resume_branch_url, possible_transports=possible_transports
            )
        except BranchUnavailable as e:
            logger.info('Resume branch URL: %s', e.url)
            traceback.print_exc()
            raise WorkerFailure(
                "worker-resume-branch-unavailable", str(e),
                details={'url': e.url})
        except BranchMissing as e:
            raise WorkerFailure(
                "worker-resume-branch-missing", str(e),
                details={'url': e.url})
        else:
            logger.info("Resuming from branch %s", full_branch_url(resume_branch))
    else:
        resume_branch = None

    ws = Workspace(
        main_branch,
        resume_branch=resume_branch,
        cached_branch=cached_branch,
        path=os.path.join(output_directory, build_target.directory_name()),
        additional_colocated_branches=(
            build_target.additional_colocated_branches(main_branch)
        ),
        resume_branch_additional_colocated_branches=(
            [n for (f, n) in extra_resume_branches] if extra_resume_branches else None
        ),
    )

    try:
        ws.__enter__()
    except IncompleteRead as e:
        traceback.print_exc()
        raise WorkerFailure("worker-clone-incomplete-read", str(e))
    except MalformedTransform as e:
        traceback.print_exc()
        raise WorkerFailure("worker-clone-malformed-transform", str(e))
    except UnexpectedHttpStatus as e:
        traceback.print_exc()
        if e.code == 502:
            raise WorkerFailure("worker-clone-bad-gateway", str(e))
        else:
            raise WorkerFailure("worker-clone-http-%s" % e.code, str(e))

    try:
        logger.info('Workspace ready - starting.')

        if ws.local_tree.has_changes():
            if list(ws.local_tree.iter_references()):
                raise WorkerFailure(
                    "requires-nested-tree-support",
                    "Missing support for nested trees in Breezy.",
                )
            raise AssertionError

        metadata["revision"] = metadata[
            "main_branch_revision"
        ] = ws.main_branch.last_revision().decode()

        metadata["subworker"] = {}
        metadata["remotes"] = {}

        def provide_context(c):
            metadata["context"] = c

        if ws.resume_branch is None:
            # If the resume branch was discarded for whatever reason, then we
            # don't need to pass in the subworker result.
            resume_subworker_result = None

        reporter = WorkerReporter(
            metadata["subworker"],
            resume_subworker_result,
            provide_context,
            metadata["remotes"],
        )

        reporter.report_remote("origin", main_branch.user_url)

        try:
            changer_result = build_target.make_changes(
                ws.local_tree, subpath, reporter, output_directory,
                committer=committer
            )
        except WorkerFailure as e:
            if e.code == "nothing-to-do":
                if resume_subworker_result is not None:
                    raise WorkerFailure("nothing-new-to-do", e.description)
                elif force_build:
                    changer_result = ChangerResult(
                        description='No change build',
                        mutator=None,
                        branches=[],
                        tags={},
                        value=0)
                else:
                    raise
            else:
                raise
        finally:
            metadata["revision"] = ws.local_tree.branch.last_revision().decode()

        actual_command = _drop_env(command)

        logging.info('Actual command: %r', actual_command)

        if force_build:
            should_build = True
        else:
            if not changer_result.branches:
                raise WorkerFailure("nothing-to-do", "Nothing to do.")

            should_build = (
                any([(role is None or role == 'main')
                     for (role, name, br, r) in changer_result.branches]))

        if should_build:
            build_target_details = build_target.build(
                ws, subpath, output_directory, env)
        else:
            build_target_details = None

        branches: Optional[List[Tuple[str, str, bytes, bytes]]]
        if changer_result.branches is not None:
            branches = [
                (f, n or main_branch.name, br, r)  # type: ignore
                for (f, n, br, r) in changer_result.branches
            ]
            if not ws.refreshed and extra_resume_branches:
                # Preserve resume branches that weren't returned by the worker
                for (f, n) in extra_resume_branches:
                    if any([b[1] == n for b in branches]):
                        continue
                    try:
                        br = resume_branch.controldir.open_branch(n).last_revision()
                    except NotBranchError:
                        br = None
                    branches.append(
                        (f, n,
                         br,
                         ws.local_tree.controldir.open_branch(n).last_revision()))
        else:
            branches = None

        wr = WorkerResult(
            changer_result.description,
            changer_result.value,
            branches,
            changer_result.tags,
            build_target.name, build_target_details,
            subworker=changer_result.mutator
        )
        yield ws, wr
    except BaseException:
        if ws.__exit__(*sys.exc_info()) is not True:
            raise
    else:
        ws.__exit__(None, None, None)


async def abort_run(
        session: ClientSession, base_url: str, run_id: str,
        metadata: Any, description: str) -> None:
    metadata['code'] = 'aborted'
    metadata['description'] = description
    finish_time = datetime.utcnow()
    metadata["finish_time"] = finish_time.isoformat()

    try:
        await upload_results(session, base_url, run_id, metadata, None)
    except ResultUploadFailure as e:
        logging.warning('Result upload for abort failed: %s', e)


def handle_sigterm(session, base_url, run_id, metadata):
    logging.warning('Received signal, aborting and exiting...')

    async def shutdown():
        await abort_run(
            session, base_url, run_id, metadata, "Killed by signal")
        sys.exit(1)
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown())


@contextmanager
def bundle_results(metadata: Any, directory: Optional[str] = None):
    with ExitStack() as es:
        with MultipartWriter("form-data") as mpwriter:
            mpwriter.append_json(
                metadata,
                headers=[  # type: ignore
                    (
                        "Content-Disposition",
                        'attachment; filename="result.json"; '
                        "filename*=utf-8''result.json",
                    )
                ],
            )  # type: ignore
            if directory is not None:
                for entry in os.scandir(directory):
                    if entry.is_file():
                        f = open(entry.path, "rb")
                        es.enter_context(f)
                        mpwriter.append(
                            BytesIO(f.read()),
                            headers=[  # type: ignore
                                (
                                    "Content-Disposition",
                                    'attachment; filename="%s"; '
                                    "filename*=utf-8''%s" % (entry.name, entry.name),
                                )
                            ],
                        )  # type: ignore
        yield mpwriter


async def upload_results(
    session: ClientSession,
    base_url: str,
    run_id: str,
    metadata: Any,
    output_directory: Optional[str] = None,
) -> Any:
    with bundle_results(metadata, output_directory) as mpwriter:
        finish_url = urljoin(base_url, "active-runs/%s/finish" % run_id)
        async with session.post(
            finish_url, data=mpwriter, timeout=DEFAULT_UPLOAD_TIMEOUT
        ) as resp:
            if resp.status == 404:
                resp_json = await resp.json()
                raise ResultUploadFailure(resp_json["reason"])
            if resp.status not in (201, 200):
                raise ResultUploadFailure(
                    "Unable to submit result: %r: %d" % (await resp.text(), resp.status)
                )
            return await resp.json()


@contextmanager
def copy_output(output_log: str):
    old_stdout = os.dup(sys.stdout.fileno())
    old_stderr = os.dup(sys.stderr.fileno())
    p = subprocess.Popen(["tee", output_log], stdin=subprocess.PIPE)
    os.dup2(p.stdin.fileno(), sys.stdout.fileno())  # type: ignore
    os.dup2(p.stdin.fileno(), sys.stderr.fileno())  # type: ignore
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(old_stdout, sys.stdout.fileno())
        os.dup2(old_stderr, sys.stderr.fileno())
        p.stdin.close()  # type: ignore


def push_branch(
    source_branch: Branch,
    url: str,
    vcs_type: str,
    overwrite=False,
    stop_revision=None,
    tag_selector=None,
    possible_transports: Optional[List[Transport]] = None,
) -> None:
    url, params = urlutils.split_segment_parameters(url)
    branch_name = params.get("branch")
    if branch_name is not None:
        branch_name = urlutils.unquote(branch_name)
    if vcs_type is None:
        vcs_type = source_branch.controldir.cloning_metadir()
    try:
        target = ControlDir.open(url, possible_transports=possible_transports)
    except NotBranchError:
        target = ControlDir.create(
            url, format=vcs_type, possible_transports=possible_transports
        )

    target.push_branch(
        source_branch, revision_id=stop_revision, overwrite=overwrite, name=branch_name,
        tag_selector=tag_selector
    )


def _push_error_to_worker_failure(e):
    if isinstance(e, UnexpectedHttpStatus):
        if e.code == 502:
            return WorkerFailure(
                "result-push-bad-gateway",
                "Failed to push result branch: %s" % e,
            )
        return WorkerFailure(
            "result-push-failed", "Failed to push result branch: %s" % e
        )
    if (isinstance(e, InvalidHttpResponse) or
            isinstance(e, IncompleteRead) or
            isinstance(e, MirrorFailure) or
            isinstance(e, ConnectionError)):
        return WorkerFailure(
            "result-push-failed", "Failed to push result branch: %s" % e
        )
    if isinstance(e, RemoteGitError):
        if str(e) == 'missing necessary objects':
            return WorkerFailure(
                'result-push-git-missing-necessary-objects', str(e))
        elif str(e) == 'failed to updated ref':
            return WorkerFailure(
                'result-push-git-ref-update-failed',
                str(e))
        else:
            return WorkerFailure("result-push-git-error", str(e))
    return e


def run_worker(
    branch_url,
    run_id,
    subpath,
    vcs_type,
    env,
    command,
    output_directory,
    metadata,
    vcs_manager,
    vendor,
    suite,
    target,
    resume_branch_url=None,
    cached_branch_url=None,
    resume_subworker_result=None,
    resume_branches=None,
    possible_transports=None,
    force_build=False
):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with copy_output(os.path.join(output_directory, "worker.log")):
        try:
            with process_package(
                branch_url,
                subpath,
                env,
                command,
                output_directory,
                metadata=metadata,
                target=target,
                resume_branch_url=resume_branch_url,
                cached_branch_url=cached_branch_url,
                resume_subworker_result=resume_subworker_result,
                extra_resume_branches=[
                    (role, name) for (role, name, base, revision) in resume_branches
                ]
                if resume_branches
                else None,
                possible_transports=possible_transports,
                force_build=force_build
            ) as (ws, result):
                enable_tag_pushing(ws.local_tree.branch)
                logging.info("Pushing result branch to %r", vcs_manager)

                try:
                    import_branches(
                        vcs_manager,
                        ws.local_tree.branch,
                        env["PACKAGE"],
                        suite,
                        run_id,
                        result.branches,
                        result.tags,
                    )
                except Exception as e:
                    raise _push_error_to_worker_failure(e)

                logging.info("Pushing packaging branch cache to %s", cached_branch_url)

                def tag_selector(tag_name):
                    return tag_name.startswith(vendor + '/') or tag_name.startswith('upstream/')

                try:
                    push_branch(
                        ws.local_tree.branch,
                        cached_branch_url,
                        vcs_type=vcs_type.lower() if vcs_type is not None else None,
                        possible_transports=possible_transports,
                        stop_revision=ws.main_branch.last_revision(),
                        tag_selector=tag_selector,
                        overwrite=True,
                    )
                except (InvalidHttpResponse, IncompleteRead, MirrorFailure,
                        ConnectionError, UnexpectedHttpStatus, RemoteGitError) as e:
                    logging.warning(
                        "unable to push to cache URL %s: %s",
                        cached_branch_url, e)

                logging.info("All done.")
                return result
        except WorkerFailure:
            raise
        except BaseException:
            traceback.print_exc()
            raise


async def get_assignment(
    session: ClientSession,
    base_url: str,
    node_name: str,
    jenkins_metadata: Optional[Dict[str, str]],
) -> Any:
    assign_url = urljoin(base_url, "active-runs")
    build_arch = subprocess.check_output(
        ["dpkg-architecture", "-qDEB_BUILD_ARCH"]
    ).decode().strip()
    json: Any = {"node": node_name, "archs": [build_arch]}
    if jenkins_metadata:
        json["jenkins"] = jenkins_metadata
    logging.debug("Sending assignment request: %r", json)
    async with session.post(assign_url, json=json) as resp:
        if resp.status != 201:
            raise ValueError("Unable to get assignment: %r" % await resp.text())
        return await resp.json()


class WatchdogPetter(object):

    def __init__(self, base_url, auth, run_id, queue_id=None):
        self.base_url = base_url
        self.auth = auth
        self.run_id = run_id
        self._task = None
        self._log_cached = []
        self.ws = None
        self.loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        self._tasks = []
        self._log_dir_tasks = {}
        self._last_communication = datetime.utcnow()
        self.kill = None
        self.queue_id = queue_id

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self):
        for task in [self._connection(), self._send_keepalives()]:
            self._tasks.append(task)
            asyncio.run_coroutine_threadsafe(task, self.loop)

    async def _send_keepalives(self):
        try:
            while True:
                await asyncio.sleep(10)
                if (datetime.utcnow() - self._last_communication).total_seconds() > 60:
                    if not await self.send_keepalive():
                        logging.warning('failed to send keepalive')
        except BaseException:
            logging.exception('sending keepalives')
            raise

    async def _connection(self):
        ws_url = urljoin(
            self.base_url, "ws/active-runs/%s/progress" % self.run_id)
        params = {}
        if self.queue_id is not None:
            params['queue_id'] = self.queue_id
        async with ClientSession(auth=self.auth) as session:
            while True:
                try:
                    self.ws = await session.ws_connect(ws_url, params=params)
                except (ClientResponseError, ClientConnectorError) as e:
                    self.ws = None
                    logging.warning("progress ws: Unable to connect: %s" % e)
                    await asyncio.sleep(5)
                    continue

                for (fn, data) in self._log_cached:
                    await self.send_log_fragment(fn, data)
                self._log_cached = []

                while True:
                    msg = await self.ws.receive()

                    if msg.type == aiohttp.WSMsgType.text:
                        logging.warning("Unknown websocket message: %r", msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        if msg.data == b'kill':
                            logging.info('Received kill over websocket, exiting..')
                            if self.kill:
                                self.kill()
                        else:
                            logging.warning("Unknown websocket message: %r", msg.data)
                    elif msg.type == aiohttp.WSMsgType.closed:
                        break
                    elif msg.type == aiohttp.WSMsgType.error:
                        logging.warning("Error on websocket: %s", self.ws.exception())
                        break
                    elif msg.type == aiohttp.WSMsgType.close:
                        logging.info('Request to close websocket.')
                        await self.ws.close()
                        break
                    else:
                        logging.warning("Ignoring ws message type %r", msg.type)
                self.ws = None
                await asyncio.sleep(5)

    async def send_keepalive(self):
        if self.ws is not None:
            logging.debug('Sending keepalive')
            await self.ws.send_bytes(b"keepalive")
            return True
        else:
            logging.debug('Not sending keepalive; websocket is dead')
            return False
        self._last_communication = datetime.utcnow()

    async def send_log_fragment(self, filename, data):
        if self.ws is None:
            self._log_cached.append((filename, data))
        else:
            await self.ws.send_bytes(
                b"\0".join([b"log", filename.encode("utf-8"), data])
            )
        self._last_communication = datetime.utcnow()

    def track_log_directory(self, directory):
        task = self._forward_logs(directory)
        self._log_dir_tasks[directory] = task
        asyncio.run_coroutine_threadsafe(task, self.loop)

    async def _forward_logs(self, directory):
        fs = {}
        try:
            while True:
                try:
                    for entry in os.scandir(directory):
                        if (entry.name not in fs and
                                entry.name.endswith('.log')):
                            fs[entry.name] = open(entry.path, 'rb')
                except FileNotFoundError:
                    pass  # Uhm, okay
                for name, f in fs.items():
                    data = f.read()
                    await self.send_log_fragment(name, data)
                await asyncio.sleep(60)
        except BaseException:
            logging.exception('log directory forwarding')
            raise


INDEX_TEMPLATE = Template("""\
<html>
<head><title>Job</title></head>
<body>

<h1>Build Details</h1>

<ul>
<li><b>Command: </b>{{ assignment['command'] }}</li>
<li><b>Start Time: </b>: {{ metadata['start_time'] }}
<li><b>Current duration: </b>: {{ datetime.utcnow() - datetime.fromisoformat(metadata['start_time']) }}
</ul>

<h1>Logs</h1>
<ul>
{% for name in names %}
  <li><a href="/logs/{{ name }}">{{ name }}</a></li>
{% endfor %}
</ul>

</body>
</html>
""")


async def handle_index(request):
    return web.Response(text=INDEX_TEMPLATE.render(
        assignment=request.app['assignment'],
        metadata=request.app['metadata'],
        datetime=datetime),
        content_type='text/html', status=200)


async def handle_assignment(request):
    return web.json_response(request.app['assignment'])


LOG_INDEX_TEMPLATE = Template("""\
<html>
<head><title>Log Index</title><head>
<body>
<h1>Logs</h1>
<ul>
{% for name in names %}
  <li><a href="/logs/{{ name }}">{{ name }}</a></li>
{% endfor %}
</ul>
</body>
</html>
""")


async def handle_log_index(request):
    if 'directory' not in request.app:
        raise web.HTTPNotFound(text="Log directory not created yet")
    names = [entry.name for entry in os.scandir(request.app['directory'])
             if entry.name.endswith('.log')]
    return web.Response(
        text=LOG_INDEX_TEMPLATE.render(names=names), content_type='text/html',
        status=200)


async def handle_log(request):
    return web.FileResponse(os.path.join(request.app['directory'], request.match_info['filename']))


async def handle_health(request):
    return web.Response(text='ok', status=200)


async def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="janitor-pull-worker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        type=str,
        help="Base URL",
        default="https://janitor.debian.net/api/",
    )
    parser.add_argument(
        "--output-directory", type=str, help="Output directory", default="."
    )
    parser.add_argument(
        "--credentials", help="Path to credentials file (JSON).", type=str, default=None
    )
    parser.add_argument(
        "--vcs-location", help="Override VCS location.", type=str)
    parser.add_argument(
        "--debug",
        help="Print out API communication",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--prometheus", type=str, help="Prometheus push gateway to export to."
    )
    parser.add_argument(
        '--port', type=int, default=0, help="Port to use for diagnostics web server")

    # Unused, here for backwards compatibility.
    parser.add_argument('--build-command', help=argparse.SUPPRESS, type=str)
    parser.add_argument("--gcp-logging", action="store_true")
    parser.add_argument("--listen-address", type=str, default="127.0.0.1")

    args = parser.parse_args(argv)

    if args.gcp_logging:
        import google.cloud.logging
        client = google.cloud.logging.Client()
        client.get_default_handler()
        client.setup_logging()
    else:
        if args.debug:
            log_level = logging.DEBUG
        else:
            log_level = logging.INFO

        logging.basicConfig(
            level=log_level,
            format="[%(asctime)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S")

    global_config = GlobalStack()
    global_config.set("branch.fetch_tags", True)

    base_url = yarl.URL(args.base_url)

    if args.credentials:
        with open(args.credentials) as f:
            creds = json.load(f)
        auth = BasicAuth(login=creds["login"], password=creds["password"])
    elif 'WORKER_NAME' in os.environ and 'WORKER_PASSWORD' in os.environ:
        auth = BasicAuth(
            login=os.environ["WORKER_NAME"],
            password=os.environ["WORKER_PASSWORD"])
    else:
        auth = BasicAuth.from_url(base_url)

    if auth is not None:
        class WorkerCredentialStore(PlainTextCredentialStore):
            def get_credentials(
                self, protocol, host, port=None, user=None, path=None, realm=None
            ):
                if host == base_url.host:
                    return {
                        "user": auth.login,
                        "password": auth.password,
                        "protocol": protocol,
                        "port": port,
                        "host": host,
                        "realm": realm,
                        "verify_certificates": True,
                    }
                return None

        credential_store_registry.register(
            "janitor-worker", WorkerCredentialStore, fallback=True
        )

    if any(
        filter(
            os.environ.__contains__,
            ["BUILD_URL", "EXECUTOR_NUMBER", "BUILD_ID", "BUILD_NUMBER"],
        )
    ):
        jenkins_metadata = {
            "build_url": os.environ.get("BUILD_URL"),
            "executor_number": os.environ.get("EXECUTOR_NUMBER"),
            "build_id": os.environ.get("BUILD_ID"),
            "build_number": os.environ.get("BUILD_NUMBER"),
        }
    else:
        jenkins_metadata = None

    node_name = os.environ.get("NODE_NAME")
    if not node_name:
        node_name = socket.gethostname()

    async with ClientSession(auth=auth) as session:
        try:
            assignment = await get_assignment(
                session, args.base_url, node_name, jenkins_metadata=jenkins_metadata
            )
        except asyncio.TimeoutError as e:
            logging.fatal("timeout while retrieving assignment: %s", e)
            return 1

        logging.debug("Got back assignment: %r", assignment)

        watchdog_petter = WatchdogPetter(
            args.base_url, auth, assignment['id'],
            queue_id=assignment['queue_id'])
        watchdog_petter.start()

        suite = assignment["suite"]
        branch_url = assignment["branch"]["url"]
        vcs_type = assignment["branch"]["vcs_type"]
        force_build = assignment.get('force-build', False)
        subpath = assignment["branch"].get("subpath", "") or ""
        if assignment["resume"]:
            resume_result = assignment["resume"].get("result")
            resume_branch_url = assignment["resume"]["branch_url"].rstrip("/")
            resume_branches = [
                (role, name, base.encode("utf-8"), revision.encode("utf-8"))
                for (role, name, base, revision) in assignment["resume"]["branches"]
            ]
        else:
            resume_result = None
            resume_branch_url = None
            resume_branches = None
        cached_branch_url = assignment["branch"].get("cached_url")
        command = assignment["command"]
        if isinstance(command, str):
            command = shlex.split(command)
        target = assignment["build"]["target"]
        build_environment = assignment["build"].get("environment", {})

        start_time = datetime.utcnow()
        metadata = {
            "queue_id": assignment["queue_id"],
            "start_time": start_time.isoformat()
        }
        if jenkins_metadata:
            metadata["jenkins"] = jenkins_metadata

        if args.vcs_location:
            vcs_manager = LocalVcsManager(args.vcs_location)
        else:
            vcs_manager = RemoteVcsManager.from_urls(assignment["vcs_store"])

        run_id = assignment["id"]

        possible_transports = []

        env = assignment["env"]

        vendor = build_environment.get('DEB_VENDOR', 'debian')

        os.environ.update(env)
        os.environ.update(build_environment)

        with TemporaryDirectory(prefix='janitor') as output_directory:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(
                signal.SIGINT, handle_sigterm, session, args.base_url,
                run_id, metadata)
            loop.add_signal_handler(
                signal.SIGTERM, handle_sigterm, session, args.base_url,
                run_id, metadata)
            app = web.Application()
            app['directory'] = output_directory
            app['assignment'] = assignment
            app['metadata'] = metadata
            app.router.add_get('/', handle_index, name='index')
            app.router.add_get('/assignment', handle_assignment, name='assignment')
            app.router.add_get('/logs/', handle_log_index, name='log-index')
            app.router.add_get('/logs/{filename}', handle_log, name='log')
            app.router.add_get('/health', handle_health, name='health')
            setup_metrics(app)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, args.listen_address, args.port)
            await site.start()
            (site_addr, site_port) = site._server.sockets[0].getsockname()
            logging.info('Diagnostics available at http://%s:%d/', site_addr, site_port)
            watchdog_petter.track_log_directory(output_directory)

            main_task = loop.run_in_executor(
                None,
                partial(
                    run_worker,
                    branch_url,
                    run_id,
                    subpath,
                    vcs_type,
                    os.environ,
                    command,
                    output_directory,
                    metadata,
                    vcs_manager,
                    vendor,
                    suite,
                    target=target,
                    resume_branch_url=resume_branch_url,
                    resume_branches=resume_branches,
                    cached_branch_url=cached_branch_url,
                    resume_subworker_result=resume_result,
                    possible_transports=possible_transports,
                    force_build=force_build
                ),
            )
            watchdog_petter.kill = main_task.cancel
            try:
                result = await main_task
            except WorkerFailure as e:
                metadata.update(e.json())
                logging.info("Worker failed (%s): %s", e.code, e.description)
                # This is a failure for the worker, but returning 0 will cause
                # jenkins to mark the job having failed, which is not really
                # true.  We're happy if we get to successfully POST to /finish
                return 0
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    metadata["code"] = "no-space-on-device"
                    metadata["description"] = str(e)
                else:
                    metadata["code"] = "worker-exception"
                    metadata["description"] = str(e)
                    raise
            except BaseException as e:
                metadata["code"] = "worker-failure"
                metadata["description"] = ''.join(traceback.format_exception_only(type(e), e)).rstrip('\n')
                raise
            else:
                metadata["code"] = None
                metadata.update(result.json())
                logging.info("%s", result.description)

                return 0
            finally:
                finish_time = datetime.utcnow()
                metadata["finish_time"] = finish_time.isoformat()
                logging.info("Elapsed time: %s", finish_time - start_time)

                try:
                    result = await upload_results(
                        session,
                        args.base_url,
                        assignment["id"],
                        metadata,
                        output_directory,
                    )
                except ResultUploadFailure as e:
                    sys.stderr.write(str(e))
                    sys.exit(1)

                logging.info('Results uploaded')

                if args.debug:
                    logging.debug("Result: %r", result)

                if args.prometheus:
                    push_to_gateway(
                        args.prometheus, job="janitor.worker",
                        registry=REGISTRY)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
