# Copyright 2020 Least Authority TFA GmbH
# See COPYING for details.

from __future__ import (
    absolute_import,
    division,
    print_function,
)

"""
Common fixtures to let the test suite focus on application logic.
"""

from __future__ import (
    absolute_import,
)

from errno import (
    ENOENT,
)

from ..util.encoding import load_yaml, dump_yaml

import attr

from fixtures import (
    Fixture,
)
from hyperlink import (
    DecodedURL,
)
from treq.client import HTTPClient
from treq.testing import StubTreq
from twisted.python.filepath import FilePath
from twisted.web.resource import Resource

from ..client import create_testing_http_client
from ..config import GlobalConfigDatabase, create_testing_configuration
from ..testing.web import (
    create_fake_tahoe_root,
    create_tahoe_treq_client,
)
from ..tahoe_client import (
    create_tahoe_client,
)
from ..magic_folder import (
    RemoteSnapshotCreator,
)
from ..status import (
    WebSocketStatusService,
)
from ..service import MagicFolderService

from ..config import (
    SQLite3DatabaseLocation,
    MagicFolderConfig,
)

@attr.s
class NodeDirectory(Fixture):
    """
    Provide just enough filesystem state to appear to be a Tahoe-LAFS node
    directory.
    """
    path = attr.ib()
    token = attr.ib(default=b"123")

    @property
    def tahoe_cfg(self):
        return self.path.child(u"tahoe.cfg")

    @property
    def node_url(self):
        return self.path.child(u"node.url")

    @property
    def magic_folder_url(self):
        return self.path.child(u"magic-folder.url")

    @property
    def private(self):
        return self.path.child(u"private")

    @property
    def api_auth_token(self):
        return self.private.child(u"api_auth_token")

    @property
    def magic_folder_yaml(self):
        return self.private.child(u"magic_folders.yaml")

    def create_magic_folder(
            self,
            folder_name,
            collective_dircap,
            upload_dircap,
            directory,
            poll_interval,
    ):
        try:
            magic_folder_config_bytes = self.magic_folder_yaml.getContent()
        except IOError as e:
            if e.errno == ENOENT:
                magic_folder_config = {}
            else:
                raise
        else:
            magic_folder_config = load_yaml(magic_folder_config_bytes)

        magic_folder_config.setdefault(
            u"magic-folders",
            {},
        )[folder_name] = {
            u"collective_dircap": collective_dircap,
            u"upload_dircap": upload_dircap,
            u"directory": directory.path,
            u"poll_interval": u"{}".format(poll_interval),
        }
        self.magic_folder_yaml.setContent(dump_yaml(magic_folder_config))

    def _setUp(self):
        self.path.makedirs()
        self.tahoe_cfg.touch()
        self.node_url.setContent(b"http://127.0.0.1:9876/")
        self.magic_folder_url.setContent(b"http://127.0.0.1:5432/")
        self.private.makedirs()
        self.api_auth_token.setContent(self.token)


class RemoteSnapshotCreatorFixture(Fixture):
    """
    A fixture which provides a ``RemoteSnapshotCreator`` connected to a
    ``MagicFolderConfig``.
    """
    def __init__(self, temp, author, upload_dircap, root=None):
        """
        :param FilePath temp: A path where the fixture may write whatever it
            likes.

        :param LocalAuthor author: The author which will be used to sign
            snapshots the ``RemoteSnapshotCreator`` creates.

        :param bytes upload_dircap: The Tahoe-LAFS capability for a writeable
            directory into which new snapshots will be linked.

        :param IResource root: The root resource for the fake Tahoe-LAFS HTTP
            API hierarchy.  The default is one created by
            ``create_fake_tahoe_root``.
        """
        if root is None:
            root = create_fake_tahoe_root()
        self.temp = temp
        self.author = author
        self.upload_dircap = upload_dircap
        self.root = root
        self.http_client = create_tahoe_treq_client(self.root)
        self.tahoe_client = create_tahoe_client(
            DecodedURL.from_text(u"http://example.com"),
            self.http_client,
        )

    def _setUp(self):
        self.magic_path = self.temp.child(b"magic")
        self.magic_path.makedirs()

        self.stash_path = self.temp.child(b"stash")
        self.stash_path.makedirs()

        self.poll_interval = 1

        self.config = MagicFolderConfig.initialize(
            u"some-folder",
            SQLite3DatabaseLocation.memory(),
            self.author,
            self.stash_path,
            u"URI:DIR2-RO:aaa:bbb",
            u"URI:DIR2:ccc:ddd",
            self.magic_path,
            self.poll_interval,
        )

        self.remote_snapshot_creator = RemoteSnapshotCreator(
            config=self.config,
            local_author=self.author,
            tahoe_client=self.tahoe_client,
            upload_dircap=self.upload_dircap,
            status=WebSocketStatusService(),
        )

@attr.s
class MagicFolderNode(object):
    http_client = attr.ib(validator=attr.validators.instance_of(HTTPClient))
    global_service = attr.ib(validator=attr.validators.instance_of(MagicFolderService))
    global_config = attr.ib(validator=attr.validators.instance_of(GlobalConfigDatabase))

    @classmethod
    def create(
        cls,
        reactor,
        basedir,
        auth_token=None,
        folders=None,
        start_folder_services=False,
        tahoe_client=None,
    ):
        """
        Create a :py:`MagicFolderService` and a treq client which is hooked up to it.

        :param reactor: A reactor to give to the ``MagicFolderService`` which will
            back the HTTP interface.

        :param FilePath basedir: A non-existant directory to create and populate
            with a new Magic Folder service configuration.

        :param unicode auth_token: The authorization token accepted by the
            service.

        :param folders: A mapping from Magic Folder names to their configurations.
            These are the folders which will appear to exist.

        :param bool start_folder_services: If ``True``, start the Magic Folder
            service objects.  Otherwise, don't.

        :param TahoeClient tahoe_client: if provided, used as the
            tahoe-client. If it is not provided, an 'empty' Tahoe client is
            provided (which is likely to cause errors if any Tahoe endpoitns
            are called via this test).

        :return MagicFolderNode:
        """
        global_config = create_testing_configuration(
            basedir,
            FilePath(u"/non-tahoe-directory"),
        )
        if auth_token is None:
            auth_token = global_config.api_token
        if folders:
            for name, config in folders.items():
                global_config.create_magic_folder(
                    name,
                    config[u"magic-path"],
                    config[u"author"],
                    config[u"collective-dircap"],
                    config[u"upload-dircap"],
                    config[u"poll-interval"],
                )

        if tahoe_client is None:
            # the caller must provide a properly-set-up Tahoe client if
            # they care about Tahoe responses. Since they didn't, an
            # "empty" one is sufficient.
            tahoe_client = create_tahoe_client(DecodedURL.from_text(u""), StubTreq(Resource()))
        status_service = WebSocketStatusService()
        global_service = MagicFolderService(
            reactor,
            global_config,
            status_service,
            # Provide a TahoeClient so MagicFolderService doesn't try to look up a
            # Tahoe-LAFS node URL in the non-existent directory we supplied above
            # in its efforts to create one itself.
            tahoe_client,
        )

        # TODO: This should be in Fixture._setUp, along with a .addCleanup(stopService)
        # See https://github.com/LeastAuthority/magic-folder/issues/334
        if start_folder_services:
            # Reach in and start the individual service for the folder we're going
            # to interact with.  This is required for certain functionality, eg
            # snapshot creation.  We avoid starting the whole global_service
            # because it wants to do error-prone things like bind ports.
            for name in folders:
                global_service.get_folder_service(name).startService()

        http_client = create_testing_http_client(reactor, global_config, global_service, lambda: auth_token, tahoe_client, status_service)

        return cls(
            http_client=http_client,
            global_service=global_service,
            global_config=global_config,
        )
