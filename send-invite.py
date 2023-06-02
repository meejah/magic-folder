from magic_folder.invite import InMemoryInviteManager
from magic_folder.testing.web import (
    create_fake_tahoe_root,
    create_tahoe_treq_client,
)
from magic_folder.tahoe_client import (
    create_tahoe_client,
)
from magic_folder.config import (
    create_testing_configuration,
)
from magic_folder.snapshot import (
    create_local_author,
)
from magic_folder.util.capabilities import random_dircap, Capability
from twisted.python.filepath import FilePath
from twisted.internet.defer import ensureDeferred
from twisted.internet.task import react
from hyperlink import (
    DecodedURL,
)
import wormhole


class FakeStatus:
    def error_occurred(self, err):
        print(f"ERROR: {err}")


async def main(reactor):
    status = FakeStatus()
    global_config = create_testing_configuration(
        FilePath("./fakeconfig"),
        FilePath(u"/no/tahoe/node-directory"),
    )
    magic_path = FilePath("./fakemagic")
    try:
        magic_path.makedirs()
    except:
        pass
    tahoe_root = create_fake_tahoe_root()

    from json import dumps
    data = dumps([
        "dirnode",
        {
            "mutable": True,
            "children": {
            }
        }
    ]).encode("utf8")

    collective_cap = Capability.from_string(
        tahoe_root.add_mutable_data(u"URI:DIR2-RO:", data)[1]
    )
    personal_cap = Capability.from_string(
        tahoe_root.add_mutable_data(u"URI:DIR2:", b"")[1]
    )
    config = global_config.create_magic_folder(
        u"foldername",
        magic_path,
        create_local_author(u"Margaret Hamilton"),
        collective_cap,
        personal_cap,
        60,
        None,
    )
    tahoe_client = create_tahoe_client(
        DecodedURL.from_text(u"http://invalid./"),
        create_tahoe_treq_client(tahoe_root),
    )
    mgr = InMemoryInviteManager(
        tahoe_client,
        status,
        config,
    )
    print(mgr)
    wh = wormhole.create(
        appid=u"private.storage/magic-folder/invites",
        relay_url="ws://relay.magic-wormhole.io:4000/v1",
        reactor=reactor,
        versions={
            "magic-folder": {
                "supported-messages": [
                    "invite-v1",
                ],
            },
        },
    )
    invite = mgr.create_invite(reactor, "participant name", "read-only", wh)
    print(invite)
    await invite.await_code()
    print("CODE", invite.wormhole_code)
    await invite.await_done()

if __name__ == "__main__":
    react(
        lambda r: ensureDeferred(main(r))
    )
