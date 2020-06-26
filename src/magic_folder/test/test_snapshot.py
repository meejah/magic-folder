import io
import os
from tempfile import mktemp
from shutil import rmtree

from nacl.signing import (
    SigningKey,
    VerifyKey,
)
from nacl.exceptions import (
    BadSignatureError,
)

from testtools.matchers import (
    Equals,
    Contains,
    MatchesStructure,
    AfterPreprocessing,
    Always,
)
from testtools.twistedsupport import (
    succeeded,
    failed,
)
from testtools import (
    ExpectedException,
)

from hyperlink import (
    DecodedURL,
)

from hypothesis import (
    given,
)
from hypothesis.strategies import (
    binary,
)

from twisted.python.filepath import (
    FilePath,
)
from twisted.internet.defer import (
    inlineCallbacks,
)

from allmydata.client import (
    read_config,
)
from allmydata.testing.web import (
    create_fake_tahoe_root,
    create_tahoe_treq_client,
)

from .fixtures import (
    NodeDirectory,
)
from .common import (
    SyncTestCase,
    AsyncTestCase,
)
from .strategies import (
    magic_folder_filenames,
)
from magic_folder.snapshot import (
    create_local_author,
    create_local_author_from_config,
    create_author_from_json,
    write_local_author,
    create_snapshot,
    create_snapshot_from_capability,
    write_snapshot_to_tahoe,
)
from magic_folder.tahoe_client import (
    create_tahoe_client,
)


class TestLocalAuthor(SyncTestCase):
    """
    Functionaltiy of LocalAuthor instances
    """

    def setUp(self):
        d = super(TestLocalAuthor, self).setUp()
        magic_dir = FilePath(mktemp())
        self.node = self.useFixture(NodeDirectory(FilePath(mktemp())))
        self.node.create_magic_folder(
            u"default",
            u"URI:CHK2:{}:{}:1:1:256".format(u"a"*16, u"a"*32),
            u"URI:CHK2:{}:{}:1:1:256".format(u"b"*16, u"b"*32),
            magic_dir,
            60,
        )

        self.config = read_config(self.node.path.path, "portnum")

        return d

    def test_serialize_author(self):
        """
        Write and then read a LocalAuthor to our node-directory
        """
        alice = create_local_author("alice")
        self.assertThat(alice.name, Equals("alice"))

        # serialize the author to disk
        write_local_author(alice, "default", self.config)

        # read back the author
        alice2 = create_local_author_from_config(self.config)
        self.assertThat(
            alice2,
            MatchesStructure(
                name=Equals("alice"),
                verify_key=Equals(alice.verify_key),
            )
        )


class TestRemoteAuthor(AsyncTestCase):
    """
    Test serialization (to/from JSON) of RemoteAuthor
    """

    def setUp(self):
        """
        We have Alices's signing+verify key
        """
        d = super(TestRemoteAuthor, self).setUp()
        self.alice = create_local_author("alice")
        return d

    def test_author_serialize(self):
        js = self.alice.to_remote_author().to_json()
        alice2 = create_author_from_json(js)

        self.assertThat(
            alice2,
            MatchesStructure(
                name=Equals(self.alice.name),
                verify_key=Equals(self.alice.verify_key),
            )
        )

    def test_author_serialize_extra_data(self):
        js = {
            "name": "wrong",
            "invalid_key": 42,
        }
        with ExpectedException(ValueError, ".*key 'invalid_key'.*"):
            create_author_from_json(js)

    def test_author_serialize_missing_data(self):
        js = {
            "name": "foo",
            # mising verify_key
        }
        with ExpectedException(ValueError, ".*requires 'verify_key'.*"):
            create_author_from_json(js)


class TestLocalSnapshot(SyncTestCase):
    """
    Test functionality of LocalSnapshot, the in-memory version of Snapshots.
    """

    def setUp(self):
        self.alice = create_local_author("alice")
        self.stash_dir = mktemp()
        os.mkdir(self.stash_dir)
        return super(TestLocalSnapshot, self).setUp()

    def tearDown(self):
        rmtree(self.stash_dir)
        return super(TestLocalSnapshot, self).tearDown()

    @given(
        content=binary(min_size=1),
        filename=magic_folder_filenames(),
    )
    def test_create_new_snapshot(self, content, filename):
        """
        create a new snapshot (this will have no parent snapshots).
        """
        data = io.BytesIO(content)

        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data,
            snapshot_stash_dir=self.stash_dir,
            parents=[],
        )

        def get_data(snap):
            """
            So, what we really want to do here is to call
            snap.get_content_producer() and pull all the data out of
            that ... but we can't, because testtools can't work with
            a real reactor (and the only work-around I know of is
            the _SynchronousBodyProducer from treq, but we don't want
            to use that inside Snapshot because "in the real case"
            we don't want it to produce all the data synchronously)
            ...
            so, instead, we cheat a little with a test-only method
            """
            return snap._get_synchronous_content()

        self.assertThat(
            d,
            succeeded(
                AfterPreprocessing(get_data, Equals(content))
            )
        )

    @given(
        content=binary(min_size=1),
        filename=magic_folder_filenames(),
    )
    def test_snapshot_improper_parent(self, content, filename):
        """
        a snapshot with non-LocalSnapshot parents fails
        """
        data = io.BytesIO(content)

        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data,
            snapshot_stash_dir=self.stash_dir,
            parents=["not a LocalSnapshot instance"],
        )

        self.assertThat(
            d,
            failed(
                AfterPreprocessing(
                    str,
                    Contains("Parent 0 is type <type 'str'> not LocalSnapshot")
                )
            )
        )

    @given(
        content1=binary(min_size=1),
        content2=binary(min_size=1),
        filename=magic_folder_filenames(),
    )
    def test_create_local_snapshots(self, content1, content2, filename):
        """
        Create a local snapshot and then change the content of the file
        to make another snapshot.
        """
        data1 = io.BytesIO(content1)
        parents = []

        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data1,
            snapshot_stash_dir=self.stash_dir,
        )
        d.addCallback(parents.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )

        data2 = io.BytesIO(content2)
        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data2,
            snapshot_stash_dir=self.stash_dir,
            parents=parents,
        )
        d.addCallback(parents.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )

    @given(
        content1=binary(min_size=1),
        content2=binary(min_size=1),
        filename=magic_folder_filenames(),
    )
    def test_snapshots_with_parents(self, content1, content2, filename):
        """
        Create a local snapshot, commit it to the grid, then extend that
        with another local snapshot and again commit it with the previously
        created remote snapshot as the parent. Now, fetch the remote from the
        capability string and compare parent to see if they match.
        """
        data1 = io.BytesIO(content1)
        local_snapshots = []

        # create a local snapshot and commit it to the grid
        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data1,
            snapshot_stash_dir=self.stash_dir,
            parents=[],
        )
        d.addCallback(local_snapshots.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )

        # now modify the same file and create a new local snapshot
        data2 = io.BytesIO(content2)
        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data2,
            snapshot_stash_dir=self.stash_dir,
            parents=local_snapshots,
        )

        d.addCallback(local_snapshots.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )


class TestRemoteSnapshot(AsyncTestCase):
    """
    Test upload and download of LocalSnapshot (creating RemoteSnapshot)
    """

    @inlineCallbacks
    def setUp(self):
        super(TestRemoteSnapshot, self).setUp()
        self.root = create_fake_tahoe_root()
        self.http_client = yield create_tahoe_treq_client(self.root)
        self.tahoe_client = yield create_tahoe_client(
            u"http://example.com",
            self.http_client,
        )
        self.alice = create_local_author("alice")
        self.stash_dir = mktemp()
        os.mkdir(self.stash_dir)

    def tearDown(self):
        rmtree(self.stash_dir)
        return super(TestRemoteSnapshot, self).tearDown()

    def _download_content(self, snapshot_cap):
        d = self.tahoe_client.download_capability(snapshot_cap)
        data = json.loads(d.result)
        content_cap = data["content"][1]["ro_uri"]
        sig = data["content"][1]["metadata"]["magic_folder"]["author_signature"]
        # XXX is it "testtools-like" to check the signature here too?
        return self.tahoe_client.download_capability(content_cap)

    @given(
        content=binary(min_size=1),
        filename=magic_folder_filenames(),
    )
    def test_snapshot_roundtrip(self, content, filename):
        """
        Create a local snapshot, write into tahoe to create a remote snapshot,
        then read back the data from the snapshot cap to recreate the remote
        snapshot and check if it is the same as the previous one.
        """
        data = io.BytesIO(content)

        snapshots = []
        # create LocalSnapshot
        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data,
            snapshot_stash_dir=self.stash_dir,
            parents=[],
        )
        d.addCallback(snapshots.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )

        # create remote snapshot
        d = write_snapshot_to_tahoe(snapshots[0], self.alice, self.tahoe_client)
        d.addCallback(snapshots.append)

        self.assertThat(
            d,
            succeeded(Always()),
        )

        # snapshots[1] is a RemoteSnapshot
        # print("remote snapshot: {}".format(snapshots[1]))

        # now, recreate remote snapshot from the cap string and compare with the original.
        # Check whether information is preserved across these changes.

        snapshot_d = create_snapshot_from_capability(snapshots[1].capability, self.tahoe_client)
        self.assertThat(snapshot_d, succeeded(Always()))
        snapshot = snapshot_d.result

        self.assertThat(snapshot, MatchesStructure(name=Equals(filename)))
        content_io = io.BytesIO()
        snapshot.fetch_content(self.tahoe_client, content_io)
        self.assertEqual(content_io.getvalue(), content)


    @given(
        content=binary(min_size=1),
        filename=magic_folder_filenames(),
    )
    def test_snapshot_remote_parent(self, content, filename):
        """
        Create a local snapshot, write into tahoe to create a remote snapshot,
        then create another local snapshot with a remote parent.
        """
        data = io.BytesIO(content)

        snapshots = []
        # create LocalSnapshot
        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data,
            snapshot_stash_dir=self.stash_dir,
            parents=[],
        )
        d.addCallback(snapshots.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )

        # snapshots[0] is a LocalSnapshot with no parents

        # turn it into a remote snapshot by uploading
        d = write_snapshot_to_tahoe(snapshots[0], self.alice, self.tahoe_client)
        d.addCallback(snapshots.append)

        self.assertThat(
            d,
            succeeded(Always()),
        )

        # snapshots[1] is a RemoteSnapshot with no parents,
        # corresponding to snapshots[0]

        d = create_snapshot(
            name=filename,
            author=self.alice,
            data_producer=data,
            snapshot_stash_dir=self.stash_dir,
            parents=[snapshots[1]],
        )
        d.addCallback(snapshots.append)
        self.assertThat(
            d,
            succeeded(Always()),
        )
        self.assertThat(
            snapshots[2],
            MatchesStructure(
                name=Equals(filename),
                parents_remote=AfterPreprocessing(len, Equals(1)),
            )
        )

        # upload snapshots[2], turning it into a RemoteSnapshot
        # .. which should have one parent

        d = write_snapshot_to_tahoe(snapshots[2], self.alice, self.tahoe_client)
        d.addCallback(snapshots.append)

        self.assertThat(
            d,
            succeeded(Always()),
        )
        # ...the last thing we wrote is now a RemoteSnapshot and
        # should have a single parent
        self.assertThat(
            snapshots[3],
            MatchesStructure(
                name=Equals(filename),
                parents_raw=Equals([snapshots[1].capability]),
            )
        )
