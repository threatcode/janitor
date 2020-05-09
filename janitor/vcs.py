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

from io import BytesIO
import os
from typing import Optional, List, Tuple, Iterable

import urllib.parse
import breezy.git  # noqa: F401
import breezy.bzr  # noqa: F401
from breezy import urlutils
from breezy.branch import Branch
from breezy.diff import show_diff_trees
from breezy.errors import (
    ConnectionError,
    NotBranchError,
    NoSuchRevision,
    NoRepositoryPresent,
    IncompatibleRepositories,
    InvalidHttpResponse,
    )
from breezy.git.remote import RemoteGitError
from breezy.controldir import ControlDir, format_registry
from breezy.repository import Repository
from breezy.transport import Transport
from lintian_brush.vcs import (
    determine_browser_url,
    unsplit_vcs_url,
    )
from silver_platter.utils import (
    open_branch_containing,
    open_branch,
    BranchMissing,
    BranchUnavailable,
    BranchUnsupported,
    )

from .trace import note


SUPPORTED_VCSES = ['git', 'bzr']

CACHE_URL_BZR = 'https://janitor.debian.net/bzr/'
CACHE_URL_GIT = 'https://janitor.debian.net/git/'


class BranchOpenFailure(Exception):
    """Failure to open a branch."""

    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description


def get_vcs_abbreviation(repository: Repository) -> str:
    vcs = getattr(repository, 'vcs', None)
    if vcs:
        return vcs.abbreviation
    return 'bzr'


def is_alioth_url(url: str) -> bool:
    return urllib.parse.urlparse(url).netloc in (
        'svn.debian.org', 'bzr.debian.org', 'anonscm.debian.org',
        'hg.debian.org', 'git.debian.org', 'alioth.debian.org')


def _convert_branch_exception(
        vcs_url: str, e: Exception) -> Exception:
    if isinstance(e, BranchUnavailable):
        if 'http code 429: Too Many Requests' in str(e):
            code = 'too-many-requests'
        elif is_alioth_url(vcs_url):
            code = 'hosted-on-alioth'
        elif 'Unable to handle http code 401: Unauthorized' in str(e):
            code = '401-unauthorized'
        elif 'Unable to handle http code 502: Bad Gateway' in str(e):
            code = '502-bad-gateway'
        elif str(e).startswith('Subversion branches are not yet'):
            code = 'unsupported-vcs-svn'
        elif str(e).startswith('Mercurial branches are not yet'):
            code = 'unsupported-vcs-hg'
        elif str(e).startswith('Darcs branches are not yet'):
            code = 'unsupported-vcs-darcs'
        else:
            code = 'branch-unavailable'
        return BranchOpenFailure(code, str(e))
    if isinstance(e, BranchMissing):
        if str(e).startswith('Branch does not exist: Not a branch: '
                             '"https://anonscm.debian.org'):
            code = 'hosted-on-alioth'
        else:
            code = 'branch-missing'
        return BranchOpenFailure(code, str(e))
    if isinstance(e, BranchUnsupported):
        if str(e).startswith('Unsupported protocol for url '):
            if ('anonscm.debian.org' in str(e) or
                    'svn.debian.org' in str(e)):
                code = 'hosted-on-alioth'
            else:
                if 'svn://' in str(e):
                    code = 'unsupported-vcs-svn'
                elif 'cvs+pserver://' in str(e):
                    code = 'unsupported-vcs-cvs'
                else:
                    code = 'unsupported-vcs-protocol'
        else:
            if str(e).startswith('Subversion branches are not yet'):
                code = 'unsupported-vcs-svn'
            elif str(e).startswith('Darcs branches are not yet'):
                code = 'unsupported-vcs-darcs'
            else:
                code = 'unsupported-vcs'
        return BranchOpenFailure(code, str(e))

    return e


def open_branch_ext(
        vcs_url: str,
        possible_transports:
        Optional[List[Transport]] = None,
        probers=None) -> Branch:
    try:
        return open_branch(vcs_url, possible_transports, probers=probers)
    except (BranchUnavailable, BranchMissing, BranchUnsupported) as e:
        raise _convert_branch_exception(vcs_url, e)


def open_branch_containing_ext(
        vcs_url: str,
        possible_transports: Optional[List[Transport]] = None,
        probers=None) -> Tuple[Branch, str]:
    try:
        return open_branch_containing(
            vcs_url, possible_transports, probers=probers)
    except (BranchUnavailable, BranchMissing, BranchUnsupported) as e:
        raise _convert_branch_exception(vcs_url, e)


class MirrorFailure(Exception):
    """Branch failed to mirror."""

    def __init__(self, branch_name: str, reason: str):
        self.branch_name = branch_name
        self.reason = reason


def mirror_branches(vcs_result_dir: str, pkg: str,
                    branch_map: Iterable[Tuple[str, Branch]],
                    public_master_branch: Optional[Branch] = None) -> None:
    vcses = set(get_vcs_abbreviation(br.repository) for name, br in branch_map)
    if len(vcses) == 0:
        return
    if len(vcses) > 1:
        raise AssertionError('more than one VCS: %r' % branch_map)
    vcs = vcses.pop()
    if vcs == 'git':
        path = os.path.join(vcs_result_dir, 'git', pkg)
        os.makedirs(path, exist_ok=True)
        try:
            vcs_result_controldir = ControlDir.open(path)
        except NotBranchError:
            vcs_result_controldir = ControlDir.create(
                path, format=format_registry.get('git-bare')())
        for (target_branch_name, from_branch) in branch_map:
            try:
                target_branch = vcs_result_controldir.open_branch(
                    name=target_branch_name)
            except NotBranchError:
                target_branch = vcs_result_controldir.create_branch(
                    name=target_branch_name)
            # TODO(jelmer): Set depth
            try:
                from_branch.push(target_branch, overwrite=True)
            except NoSuchRevision as e:
                raise MirrorFailure(target_branch_name, e)
    elif vcs == 'bzr':
        path = os.path.join(vcs_result_dir, 'bzr', pkg)
        os.makedirs(path, exist_ok=True)
        try:
            vcs_result_controldir = ControlDir.open(path)
        except NotBranchError:
            vcs_result_controldir = ControlDir.create(
                path, format=format_registry.get('bzr')())
        try:
            vcs_result_controldir.open_repository()
        except NoRepositoryPresent:
            vcs_result_controldir.create_repository(shared=True)
        for (target_branch_name, from_branch) in branch_map:
            target_branch_path = os.path.join(path, target_branch_name)
            try:
                target_branch = Branch.open(target_branch_path)
            except NotBranchError:
                target_branch = ControlDir.create_branch_convenience(
                    target_branch_path)
            if public_master_branch:
                try:
                    target_branch.set_stacked_on_url(
                        public_master_branch.user_url)
                except IncompatibleRepositories:
                    pass
            try:
                from_branch.push(target_branch, overwrite=True)
            except NoSuchRevision as e:
                raise MirrorFailure(target_branch_name, e)
    else:
        raise AssertionError('unsupported vcs %s' % vcs)


def copy_vcs_dir(main_branch: Branch,
                 local_branch: Branch,
                 vcs_result_dir: str,
                 pkg: str,
                 name: str,
                 additional_colocated_branches: Optional[List[str]] = None):
    """Publish resulting changes in VCS form.

    This creates a repository with the following branches:
     * master - the original Debian packaging branch
     * KIND - whatever command was run
     * upstream - the upstream branch (optional)
     * pristine-tar the pristine tar packaging branch (optional)
    """
    branch_map = [
        (name, local_branch),
        ('master', main_branch),
    ]
    if get_vcs_abbreviation(local_branch.repository) == 'git':
        for branch_name in (additional_colocated_branches or []):
            try:
                from_branch = local_branch.controldir.open_branch(
                    name=branch_name)
            except NotBranchError:
                continue
            branch_map.append((branch_name, from_branch))
    mirror_branches(
        vcs_result_dir, pkg, branch_map, public_master_branch=main_branch)


def get_cached_branch_url(
        vcs_type: str, package: str, branch_name: str) -> str:
    if vcs_type == 'git':
        return '%s%s,branch=%s' % (
            CACHE_URL_GIT, package, branch_name)
    elif vcs_type == 'bzr':
        return '%s%s/%s' % (
            CACHE_URL_BZR, package, branch_name)
    else:
        raise AssertionError('unknown vcs type %r' % vcs_type)


def get_cached_branch(vcs_type: str,
                      package: str,
                      branch_name: str) -> Optional[Branch]:
    url = get_cached_branch_url(vcs_type, package, branch_name)
    try:
        return Branch.open(url)
    except NotBranchError:
        return None
    except RemoteGitError:
        return None
    except InvalidHttpResponse:
        return None
    except ConnectionError as e:
        note('Unable to reach cache server: %s', e)
        return None


def get_local_vcs_branch_url(
        vcs_directory: str, vcs: str, pkg: str, branch_name: str) -> str:
    if vcs == 'git':
        return 'file:%s,branch=%s' % (
            os.path.join(vcs_directory, 'git', pkg), branch_name)
    elif vcs == 'bzr':
        return os.path.join(vcs_directory, 'bzr', pkg, branch_name)
    else:
        return None


def get_local_vcs_branch(vcs_directory: str,
                         pkg: str,
                         branch_name: str) -> Branch:
    for vcs in SUPPORTED_VCSES:
        if os.path.exists(os.path.join(vcs_directory, vcs, pkg)):
            break
    else:
        return None
    url = get_local_vcs_branch_url(vcs_directory, vcs, pkg, branch_name)
    if url is None:
        return None
    return open_branch(url)


def get_local_vcs_repo(vcs_directory: str,
                       package: str,
                       vcs_type: Optional[str] = None) -> Optional[Repository]:
    for vcs in (SUPPORTED_VCSES if not vcs_type else [vcs_type]):
        path = os.path.join(vcs_directory, vcs, package)
        if not os.path.exists(path):
            continue
        return Repository.open(path)
    return None


class VcsManager(object):

    def get_branch(self, package: str, branch_name: str,
                   vcs_type: Optional[str] = None) -> Branch:
        raise NotImplementedError(self.get_branch)

    def get_branch_url(self, package: str, branch_name: str,
                       vcs_type: str) -> str:
        raise NotImplementedError(self.get_branch_url)

    def import_branches(self,
                        main_branch: Branch, local_branch: Branch, pkg: str,
                        name: str,
                        additional_colocated_branches: List[str] = None):
        raise NotImplementedError(self.import_branches)

    def get_repository(self, package: str,
                       vcs_type: Optional[str] = None) -> Repository:
        raise NotImplementedError(self.get_repository)

    def get_vcs_type(self, package: str) -> Optional[str]:
        try:
            repo = self.get_repository(package)
        except NotBranchError:
            return None
        return get_vcs_abbreviation(repo)


class LocalVcsManager(VcsManager):

    def __init__(self, base_path: str):
        self.base_path = base_path

    def get_branch(self, package, branch_name, vcs_type=None):
        try:
            return get_local_vcs_branch(
                self.base_path, package, branch_name, vcs_type)
        except (BranchUnavailable, BranchMissing):
            return None

    def get_branch_url(self, package, branch_name, vcs_type):
        return get_local_vcs_branch_url(
            self.base_path, package, branch_name, vcs_type)

    def import_branches(self, main_branch, local_branch, pkg, name,
                        additional_colocated_branches=None):
        copy_vcs_dir(
            main_branch, local_branch, self.base_path, pkg, name,
            additional_colocated_branches=additional_colocated_branches)

    def get_repository(self, package, vcs_type=None):
        return get_local_vcs_repo(self.base_path, package, vcs_type)


class RemoteVcsManager(VcsManager):

    def __init__(self, cache_url_git: str = CACHE_URL_BZR,
                 cache_url_bzr: str = CACHE_URL_BZR):
        self.cache_url_git = cache_url_git
        self.cache_url_bzr = cache_url_bzr

    def get_branch(self, package, branch_name, vcs_type=None):
        if vcs_type:
            return get_cached_branch(package, branch_name, vcs_type)
        for vcs_type in SUPPORTED_VCSES:
            branch = get_cached_branch(package, branch_name, vcs_type)
            if branch:
                return branch
        else:
            return None

    def get_branch_url(self, package, branch_name, vcs_type):
        return get_cached_branch_url(package, branch_name, vcs_type)


def get_run_diff(vcs_manager: VcsManager, run) -> bytes:
    f = BytesIO()
    try:
        repo = vcs_manager.get_repository(run.package)
    except NotBranchError:
        repo = None
    if repo is None:
        return b'Local VCS repository for %s temporarily inaccessible' % (
            run.package.encode('ascii'))
    try:
        old_tree = repo.revision_tree(run.main_branch_revision)
    except NoSuchRevision:
        return b'Old revision %s temporarily missing' % (
            run.main_branch_revision)
    try:
        new_tree = repo.revision_tree(run.revision)
    except NoSuchRevision:
        return b'New revision %s temporarily missing' % (
            run.revision)
    show_diff_trees(old_tree, new_tree, to_file=f)
    return f.getvalue()


def bzr_to_browse_url(url: str) -> str:
    (url, params) = urlutils.split_segment_parameters(url)
    branch = params.get('branch')
    if branch:
        branch = urllib.parse.unquote(branch)
    deb_vcs_url = unsplit_vcs_url(url, branch)
    return determine_browser_url(None, deb_vcs_url)
