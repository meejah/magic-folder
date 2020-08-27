# Copyright 2020 Least Authority TFA GmbH
# See COPYING for details.

"""
Tests for the Twisted service which is responsible for a single
magic-folder.
"""

from __future__ import (
    absolute_import,
    print_function,
    division,
)
from hyperlink import (
    DecodedURL,
)
from twisted.python.filepath import (
    FilePath,
)
from twisted.application.service import (
    Service,
)
from twisted.internet import task
from hypothesis import (
    given,
    find,
)
from hypothesis.strategies import (
    binary,
    just,
    one_of,
    integers,
)
from testtools.matchers import (
    Is,
    Always,
    Equals,
)
from testtools.twistedsupport import (
    succeeded,
)
from ..magic_folder import (
    MagicFolder,
    LocalSnapshotService,
    UploaderService,
)
from ..config import (
    create_global_configuration,
)
from ..tahoe_client import (
    TahoeClient,
)
from ..testing.web import (
    create_fake_tahoe_root,
    create_tahoe_treq_client,
)

from .common import (
    SyncTestCase,
)
from .strategies import (
    relative_paths,
    local_authors,
    tahoe_lafs_dir_capabilities,
    tahoe_lafs_immutable_dir_capabilities,
    folder_names,
)
from .test_local_snapshot import (
    MemorySnapshotCreator as LocalMemorySnapshotCreator,
)
from .test_upload import (
    MemorySnapshotCreator as RemoteMemorySnapshotCreator,
)

class MagicFolderServiceTests(SyncTestCase):
    """
    Tests for ``MagicFolder``.
    """
    def test_local_snapshot_service_child(self):
        """
        ``MagicFolder`` adds the service given as ``LocalSnapshotService`` to
        itself as a child.
        """
        local_snapshot_service = Service()
        tahoe_client = object()
        reactor = object()
        name = u"local-snapshot-service-test"
        config = object()
        participants = object()
        magic_folder = MagicFolder(
            client=tahoe_client,
            config=config,
            name=name,
            local_snapshot_service=local_snapshot_service,
            uploader_service=Service(),
            initial_participants=participants,
            clock=reactor,
        )
        self.assertThat(
            local_snapshot_service.parent,
            Is(magic_folder),
        )

    @given(
        relative_target_path=relative_paths(),
        content=binary(),
    )
    def test_create_local_snapshot(self, relative_target_path, content):
        """
        ``MagicFolder.local_snapshot_service`` can be used to create a new local
        snapshot for a file in the folder.
        """
        magic_path = FilePath(self.mktemp())
        magic_path.makedirs()
        target_path = magic_path.preauthChild(relative_target_path).asBytesMode("utf-8")
        target_path.parent().makedirs(ignoreExistingDirectory=True)
        target_path.setContent(content)

        local_snapshot_creator = LocalMemorySnapshotCreator()
        local_snapshot_service = LocalSnapshotService(magic_path, local_snapshot_creator)
        clock = object()

        tahoe_client = object()
        name = u"local-snapshot-service-test"
        config = object()
        participants = object()
        magic_folder = MagicFolder(
            client=tahoe_client,
            config=config,
            name=name,
            local_snapshot_service=local_snapshot_service,
            uploader_service=Service(),
            initial_participants=participants,
            clock=clock,
        )
        magic_folder.startService()
        self.addCleanup(magic_folder.stopService)

        adding = magic_folder.local_snapshot_service.add_file(
            target_path,
        )
        self.assertThat(
            adding,
            succeeded(Always()),
        )

        self.assertThat(
            local_snapshot_creator.processed,
            Equals([target_path]),
        )

    @given(
        relative_target_path=relative_paths(),
        content=binary(),
    )
    def test_create_remote_snapshot(self, relative_target_path, content):
        """
        MagicFolder.uploader_service creates a new remote snapshot
        when a file is added into the folder.
        """
        magic_path = FilePath(self.mktemp())
        magic_path.makedirs()
        target_path = magic_path.preauthChild(relative_target_path).asBytesMode("utf-8")
        target_path.parent().makedirs(ignoreExistingDirectory=True)
        target_path.setContent(content)

        local_snapshot_creator = LocalMemorySnapshotCreator()
        local_snapshot_service = LocalSnapshotService(magic_path, local_snapshot_creator)
        clock = task.Clock()
        poll_interval = 1  # XXX: this would come from config.

        # create RemoteSnapshotCreator and UploaderService
        remote_snapshot_creator = RemoteMemorySnapshotCreator()
        uploader_service = Service()

        tahoe_client = object()
        name = u"local-snapshot-service-test"
        config = object()
        participants = object()
        magic_folder = MagicFolder(
            client=tahoe_client,
            config=config,
            name=name,
            local_snapshot_service=local_snapshot_service,
            uploader_service=uploader_service,
            initial_participants=participants,
            clock=clock,
        )
        magic_folder.startService()
        self.addCleanup(magic_folder.stopService)

        self.assertThat(
            uploader_service.running,
            Equals(True),
        )


LOCAL_AUTHOR = find(local_authors(), lambda x: True)

class MagicFolderFromConfigTests(SyncTestCase):
    """
    Tests for ``MagicFolder.from_config``.
    """
    @given(
        folder_names(),
        relative_paths(),
        relative_paths(),
        just(LOCAL_AUTHOR),
        one_of(
            tahoe_lafs_immutable_dir_capabilities(),
            tahoe_lafs_dir_capabilities(),
        ),
        tahoe_lafs_dir_capabilities(),
        integers(min_value=1),
    )
    def test_uploader_service(self, name, relative_magic_path, relative_state_path, author, collective_dircap, upload_dircap, poll_interval):
        """
        ``MagicFolder.from_config`` creates an ``UploaderService``
        which will sometimes upload snapshots using the given Tahoe
        client object.
        """
        reactor = task.Clock()

        root = create_fake_tahoe_root()
        http_client = create_tahoe_treq_client(root)
        tahoe_client = TahoeClient(
            DecodedURL.from_text(U"http://example.invalid./"),
            http_client,
        )

        basedir = FilePath(self.mktemp())
        global_config = create_global_configuration(
            basedir,
            u"tcp:-1",
            FilePath(u"/non-tahoe-directory"),
            u"tcp:-1",
        )

        magic_path = basedir.preauthChild(relative_magic_path)
        magic_path.makedirs()

        statedir = basedir.child(u"state")
        state_path = statedir.preauthChild(relative_state_path)

        config = global_config.create_magic_folder(
            name,
            magic_path,
            state_path,
            author,
            collective_dircap,
            upload_dircap,
            poll_interval,
        )

        service = MagicFolder.from_config(
            reactor,
            tahoe_client,
            name,
            global_config,
        )

