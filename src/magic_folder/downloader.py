from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

"""
Classes and services relating to the operation of the Downloader
"""

from collections import deque

import attr
from attr.validators import (
    provides,
    instance_of,
)

from zope.interface import (
    Interface,
    implementer,
)

from eliot.twisted import (
    inline_callbacks,
)
from eliot import (
    start_action,
    Message,
)

from twisted.application import (
    service,
    internet,
)
from twisted.python.filepath import (
    FilePath,
)
from twisted.python.failure import (
    Failure,
)
from twisted.internet.defer import (
    Deferred,
    DeferredQueue,
    returnValue,
    CancelledError,
)
from twisted.web.error import (
    SchemeNotSupported,
)

from .config import (
    MagicFolderConfig,
)
from .magicpath import (
    magic2path,
)
from .snapshot import (
    create_snapshot_from_capability,
)


@attr.s
@implementer(service.IService)
class RemoteSnapshotCacheService(service.Service):
    """
    When told about Snapshot capabilities we download them.

    Our work queue is ephemeral; it is not synchronized to disk and
    will vanish if we are re-started. When we download a Remote
    Snapshot we *do not* download the content, just the Snapshot
    itself.

    Being ephemeral is okay because we will retry the operation the
    next time we restart. That is, we only need the RemoteSnapshot
    cached until we decide what to do and synchronize local state.

    Anyway, we do need to download parent snapshots UNTIL we reach the
    current remotesnapshot that we've noted for that name (or run
    out of parents).

    Note: we *could* keep all this in our database .. but then we have
    to evict things from it at some point.

    XXX: the "remote-snapshots" database is kind of 'just a cache'
    too; we should be putting that information into our Personal DMD
    ... so what happens when it's out of date? (source-of-truth MUST
    be our Personal DMD ...)
    TP: That *MUST* is incorrect. It is the source of data that survives
    loss of the machine, but we treat the remote-snapshot-db as the source
    of truth.

     -> actually, maybe the local db should be the "source of truth":
    we only put entries into it if we're about to push it to Tahoe
    .. so if things don't match up, it's because we crashed before
    that happened (and so on startup, we should check and possibly
    push more things up to Tahoe)
    """
    folder_config = attr.ib()
    tahoe_client = attr.ib()
    _cached_snapshots = attr.ib(default=attr.Factory(dict))
    _queue = attr.ib(default=attr.Factory(DeferredQueue))

    @classmethod
    def from_config(cls, config, tahoe_client):
        """
        Create a RemoteSnapshotCacheService from the MagicFolder
        configuration.
        """
        return cls(config, tahoe_client)

    def get_snapshot_from_capability(self, snapshot_cap):
        """
        Add the given immutable Snapshot capability to our queue.

        When this queue item is processed, we download the Snapshot,
        then download the metadata.

        (When we have signatures this should verify the signature
        before downloading anything else)

        We also download parents of this Snapshot until we find a
        common ancestor .. meaning we keep downloading parents until
        we find one that matches the remotesnapshot entry in our local
        database. If we have no entry for this Snapshot we download
        all parents (which will also be the case if there is no common
        ancestor).

        :param bytes snapshot_cap: an immutable directory capability-string

        :returns Deferred[RemoteSnapshot]: a Deferred that fires with
            the RemoteSnapshot when this item has been processed (or
            errbacks if any of the downloads fail).

        :raises QueueOverflow: if our queue is full
        """
        Message.log(
            message_type="cache-service:add-remote-capability",
            snapshot_cap=snapshot_cap,
        )
        d = Deferred()
        self._queue.put((snapshot_cap, d))
        return d

    def startService(self):
        """
        Pull new work from our queue, forever.
        """
        with start_action(message_type="cache-service:start"):
            service.Service.startService(self)
            self._service_d = self._process_queue()
            Message.log(starting=True)

    @inline_callbacks
    def _process_queue(self):
        """
        Wait for a single item from the queue and process it, forever.
        """
        while True:
            try:
                (snapshot_cap, d) = yield self._queue.get()
                with start_action(action_type="cache-service:locate_snapshot",
                                  capability=snapshot_cap) as t:
                    #FIXME: We are inconsitent here on whether we
                    # wait until we've downloaded all parents
                    # in returning from add_remote_capability
                    # - if we've not seen the snapshot, we will wait
                    # - however, if we've already cached the snapshot,
                    #   we'll immediately return it, even if we are still
                    #   processing all our parents.

                    # --> (but if we've already cached it, then we've
                    # already cached its parents too..)

                    try:
                        snapshot = self._cached_snapshots[snapshot_cap]
                        t.add_success_fields(cached=True)
                    except KeyError:
                        t.add_success_fields(cached=False)
                        snapshot = yield self._cache_snapshot(snapshot_cap)
                    d.callback(snapshot)
            except CancelledError:
                break
            except Exception:
                d.errback(Failure())

    @inline_callbacks
    def _cache_snapshot(self, snapshot_cap):
        """
        Internal helper.

        :param bytes snapshot_cap: capability-string of a Snapshot

        Cache a single snapshot, which we shall return. We also cache
        parent snapshots until we find 'ours' -- that is, whatever our
        config's remotesnapshot table points at for this name. (If we
        don't have an entry for it yet, we cache all parents .. which
        will also be the case when we don't find 'our' snapshot at
        all).
        """
        snapshot = yield create_snapshot_from_capability(
            snapshot_cap,
            self.tahoe_client,
        )
        self._cached_snapshots[snapshot_cap] = snapshot
        Message.log(message_type="remote-cache:cached",
                    name=snapshot.name,
                    capability=snapshot.capability)

        # breadth-first traversal of the parents
        q = deque([snapshot])
        while q:
            snap = q.popleft()
            for i in range(len(snap.parents_raw)):
                # FIXME: we may have already cached some of the parents
                parent = yield snap.fetch_parent(self.tahoe_client, i)
                self._cached_snapshots[parent.capability] = parent
                q.append(parent)

        returnValue(snapshot)

    def stopService(self):
        """
        Don't process queued items anymore.
        """
        d = self._service_d
        self._service_d.cancel()
        service.Service.stopService(self)
        self._service_d = None
        return d


class IMagicFolderFilesystem(Interface):
    """
    An object that can make changes to the local filesystem
    magic-directory. It has a staging area to put files into so that
    there are no 'partial' files in the magic-folder.
    """

    def download_content_to_staging(remote_snapshot, tahoe_client):
        """
        Prepare the content by downloading it.

        :returns Deferred[FilePath]: the location of the downloaded
            content (or errback if the download fails).
        """

    def mark_overwrite(remote_snapshot, staged_content):
        """
        This snapshot is an overwrite. Move it from the staging area over
        top of the existing file (if any) in the magic-folder.

        :param FilePath staged_content: a local path to the downloaded
            content.
        """

    def mark_conflict(remote_snapshot, staged_content):
        """
        This snapshot causes a conflict. The existing magic-folder file is
        untouched. The downloaded / prepared content shall be moved to
        a file named `<path>.theirs.<name>` where `<name>` is the
        petname of the author of the conflicting snapshot and `<path>`
        is the relative path inside the magic-folder.

        XXX can deletes conflict? if so staged_content would be None

        :param conflicting_snapshot: the RemoteSnapshot that conflicts

        :param FilePath staged_content: a local path to the downloaded
            content.
        """

    def mark_delete(remote_snapshot):
        """
        Mark this snapshot as a delete. The existing magic-folder file
        shall be deleted.
        """


def is_ancestor_of(cache, cap, snapshot):
    """
    :param RemoteSnapshotCacheService cache: cached snapshots

    :returns bool: True if 'cap' is an ancestor of 'snapshot'
    """
    q = deque([snapshot])
    while q:
        snap = q.popleft()
        for parent_cap in snap.parents_raw:
            if cap == parent_cap:
                return True
            else:
                q.append(cache._cached_snapshots[parent_cap])
    return False


@attr.s
@implementer(service.IService)
class MagicFolderUpdaterService(service.Service):
    """
    Updates the local magic-folder when given locally-cached
    RemoteSnapshots. These RemoteSnapshot instance must have all
    relevant parents available (via the cache service).
    FIXME: we aren't guaranteed this

    "Relevant" here means all parents unless we find a common
    ancestor.
    """
    _magic_fs = attr.ib(validator=provides(IMagicFolderFilesystem))
    _config = attr.ib(validator=instance_of(MagicFolderConfig))
    _remote_cache = attr.ib(validator=instance_of(RemoteSnapshotCacheService))
    tahoe_client = attr.ib() # validator=instance_of(TahoeClient))

    @inline_callbacks
    def add_remote_snapshot(self, snapshot):
        """
        :returns Deferred: fires with None when this RemoteSnapshot has
            been processed (or errback if that fails).
        """
        with start_action(action_type="downloader:modify_filesystem"):
            yield self._process(snapshot)

    @inline_callbacks
    def _process(self, snapshot):
        """
        Internal helper.

        Determine the disposition of 'snapshot' and propose
        appropriate filesystem modifications. Once these are done,
        note the new snapshot-cap in our database and then push it to
        Tahoe (i.e. to our Personal DMD)
        """

        #FIXME we should consider a lock or something to ensure that we don't
        # try to modify the same file twice. We can do that in-proccess if
        # we also ensure that not other process is running.

        # -> no other process can run on the same magic-folders config

        # NOTE: This isn't *currently* a problem as this is only ever called one at a time.

        # -> we only will run a single one of these per magic-folder,
        #    this can only be called one at a time (that's why this
        #    was a queue before removed in b0fb1159459b04ca490cccd961ad1092f98c63e5

        with start_action(action_type="downloader:updater:process",
                          name=snapshot.name,
                          capability=snapshot.capability,
                          ) as action:
            local_path = self._config.magic_path.preauthChild(magic2path(snapshot.name))
            #FIXME: we should not download the file if we aren't going to use it
            # particularly if we do so synchronously.
            # For example, we will download the contents of out-of-date snapshots
            # every scan
            staged = yield self._magic_fs.download_content_to_staging(snapshot, self.tahoe_client)

            # see if we have existing local or remote snapshots for
            # this name already
            try:
                remote_cap = self._config.get_remotesnapshot(snapshot.name)
                # w/ no KeyError we have seen this before
                action.add_success_fields(remote=remote_cap)
            except KeyError:
                remote_cap = None

            try:
                local_snap = self._config.get_local_snapshot(snapshot.name)
                action.add_success_fields(
                    local=local_snap.content_path.path,
                )
            except KeyError:
                local_snap = None

            # note: if local_snap and remote_cap are both non-None
            # then remote_cap should already be the ancestor of
            # local_snap by definition.

            # XXX check if we're already conflicted; if so, do not continue

            # XXX not dealing with deletes yet

            is_conflict = False
            local_timestamp = None
            if local_path.exists():
                local_timestamp = local_path.getModificationTime()
                # there is a file here already .. if we have any
                # LocalSnapshots for this name, then we've got local
                # edits and it must be a conflict. If we have nothing
                # in our remotesnapshot database then we've just not
                # made a LocalSnapshot yet (so it's still a conflict).
                if local_snap is not None or remote_cap is None:
                    is_conflict = True

                else:
                    existing_snap = self._remote_cache._cached_snapshots.get(remote_cap, None)

                    assert existing_snap is not None, "Remote should be cached already"
                    assert existing_snap.capability != snapshot.capability, "already match"

                    # "If the new snapshot is a descendant of the client's
                    # existing snapshot, then this update is an 'overwrite'"

                    ancestor = is_ancestor_of(self._remote_cache, existing_snap.capability, snapshot)
                    action.add_success_fields(ancestor=ancestor)

                    if not ancestor:
                        # if the incoming remotesnapshot is actually an
                        # ancestor of _our_ snapshot, then we have nothing
                        # to do because we are newer
                        if is_ancestor_of(self._remote_cache, snapshot.capability, existing_snap):
                            return
                        is_conflict = True

            else:
                # there is no local file
                # XXX we don't handle deletes yet
                assert not local_snap and not remote_cap, "Internal inconsistency: record of a Snapshot for this name but no local file"
                is_conflict = False

            # once here, we know if we have a conflict or not. if
            # there is nothing to do, we shold have returned
            # already. so mark an overwrite or conflict into the
            # filesystem.
            if is_conflict:
                self._magic_fs.mark_conflict(snapshot, staged)
                # FIXME probably want to also record internally that
                # this is a conflict.

            else:
                # there is a longer dance described in detail in
                # https://magic-folder.readthedocs.io/en/latest/proposed/magic-folder/remote-to-local-sync.html#earth-dragons-collisions-between-local-filesystem-operations-and-downloads
                # which may do even better at reducing the window for
                # local changes to get overwritten. Currently, that
                # window is the 3 python statements between here and
                # ".mark_overwrite()"
                last_minute_change = False
                if local_timestamp is None:
                    if local_path.exists():
                        last_minute_change = True
                else:
                    local_path.changed()
                    if local_path.getModificationTime() != local_timestamp:
                        last_minute_change = True
                if last_minute_change:
                    self._magic_fs.mark_conflict(snapshot, staged)
                    # FIXME note conflict internally
                else:
                    self._magic_fs.mark_overwrite(snapshot, staged)

                # Note, if we crash here (after moving the file into place
                # but before noting that in our database) then we could
                # produce LocalSnapshots referencing the wrong parent. We
                # will no longer produce snapshots with the wrong parent
                # once we re-run and get past this point.

                # remember the last remote we've downloaded
                self._config.store_remotesnapshot(snapshot.name, snapshot)

                # XXX careful here, we still need something that makes
                # sure mismatches between remotesnapshots in our db and
                # the Personal DMD are reconciled .. that is, if we crash
                # here and/or can't update our Personal DMD we need to
                # retry later.
                yield self.tahoe_client.add_entry_to_mutable_directory(
                    self._config.upload_dircap,
                    snapshot.name,
                    snapshot.capability.encode("ascii"),
                    replace=True,
                )


@implementer(IMagicFolderFilesystem)
@attr.s
class LocalMagicFolderFilesystem(object):
    """
    Makes changes to a local directory.
    """

    magic_path = attr.ib(validator=instance_of(FilePath))
    staging_path = attr.ib(validator=instance_of(FilePath))

    @inline_callbacks
    def download_content_to_staging(self, remote_snapshot, tahoe_client):
        """
        IMagicFolderFilesystem API
        """
        import hashlib
        h = hashlib.sha256()
        h.update(remote_snapshot.capability)
        staged_path = self.staging_path.child(h.hexdigest())
        with staged_path.open('wb') as f:
            yield tahoe_client.stream_capability(remote_snapshot.content_cap, f)
        returnValue(staged_path)

    def mark_overwrite(self, remote_snapshot, staged_content):
        """
        This snapshot is an overwrite. Move it from the staging area over
        top of the existing file (if any) in the magic-folder.

        :param FilePath staged_content: a local path to the downloaded
            content.
        """
        local_path = self.magic_path.preauthChild(magic2path(remote_snapshot.name))
        tmp = None
        if local_path.exists():
            tmp = local_path.temporarySibling(b".snap")
            local_path.moveTo(tmp)
        staged_content.moveTo(local_path)
        if tmp is not None:
            tmp.remove()

    def mark_conflict(self, remote_snapshot, staged_content):
        """
        This snapshot causes a conflict. The existing magic-folder file is
        untouched. The downloaded / prepared content shall be moved to
        a file named `<path>.theirs.<name>` where `<name>` is the
        petname of the author of the conflicting snapshot and `<path>`
        is the relative path inside the magic-folder.

        XXX can deletes conflict? if so staged_content would be None

        :param conflicting_snapshot: the RemoteSnapshot that conflicts

        :param FilePath staged_content: a local path to the downloaded
            content.
        """
        local_path = self.magic_path.preauthChild(
            magic2path(remote_snapshot.name) + ".conflict-{}".format(remote_snapshot.author.name)
        )
        staged_content.moveTo(local_path)

    def mark_delete(remote_snapshot):
        """
        Mark this snapshot as a delete. The existing magic-folder file
        shall be deleted.
        """
        raise NotImplementedError()


@implementer(IMagicFolderFilesystem)
class InMemoryMagicFolderFilesystem(object):
    """
    Simply remembers the changes that would be made to a local
    filesystem. Generally for testing.
    """

    def __init__(self):
        self.actions = []

    def download_content_to_staging(self, remote_snapshot, tahoe_client):
        self.actions.append(
            ("download", remote_snapshot)
        )

    def mark_overwrite(self, remote_snapshot, staged_content):
        self.actions.append(
            ("overwrite", remote_snapshot)
        )

    def mark_conflict(self, remote_snapshot, staged_content):
        self.actions.append(
            ("conflict", remote_snapshot)
        )

    def mark_delete(self, remote_snapshot):
        self.actions.append(
            ("delete", remote_snapshot)
        )


@attr.s
@implementer(service.IService)
class DownloaderService(service.MultiService):
    """
    A service that periodically polls the Colletive DMD for new
    RemoteSnapshot capabilities to download.
    """

    _config = attr.ib()
    _participants = attr.ib()
    _remote_snapshot_cache = attr.ib(validator=instance_of(RemoteSnapshotCacheService))
    _folder_updater = attr.ib(validator=instance_of(MagicFolderUpdaterService))
    _tahoe_client = attr.ib()

    @classmethod
    def from_config(cls, name, config, participants, remote_snapshot_cache, folder_updater, tahoe_client):
        """
        Create a DownloaderService from the MagicFolder configuration.
        """
        return cls(
            config,
            participants,
            remote_snapshot_cache,
            folder_updater,
            tahoe_client,
        )

    def __attrs_post_init__(self):
        service.MultiService.__init__(self)
        self._folder_updater.setServiceParent(self)
        self._remote_snapshot_cache.setServiceParent(self)
        self._scanner = internet.TimerService(
            self._config.poll_interval,
            # FIXME: we need to somehow catch errors here so we don't stop syncing
            # we could choose let unexepected errors here stop syncing, but
            # we'd need a way to be able to restart syncing
            self._loop,
        )
        self._scanner.setServiceParent(self)

    def _loop(self):
        d = self._scan_collective()

        def eb(failure):
            # in some cases, might want to surface elsewhere
            print(failure)

        d.addErrback(eb)
        return d

    @inline_callbacks
    def _scan_collective(self):
        action = start_action(action_type="downloader:scan-collective")
        with action:
            try:
                people = yield self._participants.list()
            except SchemeNotSupported:
                # mostly a testing aid; if we provided an "empty"
                # Tahoe Client fake we get this error .. otherwise we
                # need a proper collective set up.
                people = []
            for person in people:
                Message.log(message_type="scan-participant", person=person.name, is_self=person.is_self)
                if person.is_self:
                    # we don't download from ourselves
                    continue
                files = yield self._tahoe_client.list_directory(person.dircap)
                action.add_success_fields(files=files) #FIXME: this can't be in a loop
                for fname, data in files.items():
                    snapshot_cap, metadata = data
                    fpath = self._config.magic_path.preauthChild(magic2path(fname))
                    relpath = "/".join(fpath.segmentsFrom(self._config.magic_path))
                    action.add_success_fields(
                        #FIXME, this can't be in a loop
                        abspath=fpath.path,
                        relpath=relpath,
                    )
                    snapshot = yield self._remote_snapshot_cache.get_snapshot_from_capability(snapshot_cap)
                    # if this remote matches what we believe to be the
                    # latest, there is nothing to do .. otherwise, we
                    # have to figure out what to do
                    try:
                        our_snapshot_cap = self._config.get_remotesnapshot(snapshot.name)
                    except KeyError:
                        our_snapshot_cap = None
                    if snapshot.capability != our_snapshot_cap:
                        if our_snapshot_cap is not None:
                            yield self._remote_snapshot_cache.get_snapshot_from_capability(our_snapshot_cap)
                        yield self._folder_updater.add_remote_snapshot(snapshot)
