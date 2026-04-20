I am trying to make a hot versus cold storage management system for my self-hosted FastAPI archival system. It is a bit more complicated than I was thinking.

The main unit in the system is currently called a collection. The idea is that you want to view a collection all together as one as a directory alongside other collection directories in the hot storage.

Collections can be any size, yet they obviously have to be split up in some way to fit on the optical disks. There is a planner that handles splitting them up in the most meaningful, efficient least jarring way. When it has a planned iso available the user can download it to their local machine to burn. They then call an endpoint to indicate that a burn now exists for that iso and provide an identifier and physical location description for it.

User's upload (rclone/rsync) collections as a directory into a staging directory. Then they call an endpoint with the path that indicates that collection should be closed at which time it enter's hot storage and the planner's pool (enqueue for burning to optical when efficient disk creation is available).

The system is built around optical discs (specifically 50gb) blurays being the optical storage. On disc the files are manifested, renamed and then encrypted - file by file to be resilient against scratches on the disc and general corruption yet with one global manifest as well for convenient lookup (after you decrypt it).

Now, my main thing I am trying to figure out is how to make it convenient to phase stuff in and out of hot. I would really prefer to make hot storage read-only on disk. I will likely create a companion script that can be run locally where the disk reader is attached (the app assumes it is on a headless server without one) to interact with the encrypted disk format to easily pull individual collections, dirs within collections, and/or files within collections off of a disk by collection name and original path.

I would like to keep the app's web ui as absolutely minimal as possible though - not a file browser in any way. Preferably, just a portal to search what files/collections exist globally and what discs they are currently available on; plus, a view of the current best possible disk partitioning plan given the collections that have not yet been burned (since minimum disk size wouldn't be reachable with the current) and are cached in hot storage.

One notion I had is that the system already knows all file's hashes in the database, one could just drop whatever in a re-add-this-to-hot directory and call an endpoint and the system could add it back to hot storage at the expected path; yet, I don't see a similarly easy way to specify what you want removed from hot other than possibly a symlink mirror of hot storage with appropriate permission allowing you to delete only stuff that is already on optic and then an endpoint to indicate that you deleted some stuff and it should scan through to determine what and then make the actually deletes - which all sounds a little awkward.

Ideas? As I said, I want to keep the ui minimalist I don't want to reimplement file-browsing.

Some notes:
- I do not want the user to necessarily have to bring an entire collection back into hot storage; rather, that they can bring back even just one file. Collections could be large and split across multiple disks, so it would be awkward to make them always bring back the entire thing or nothing at all.
