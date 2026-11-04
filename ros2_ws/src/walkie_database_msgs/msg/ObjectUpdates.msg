std_msgs/Header header             # Commit time in deployment clock, frame_id=map frame.
string map_id
string store_id                    # UUID of this run/store incarnation; changes on seed restore.
uint64 sequence                    # Monotonic within store_id; a gap requires re-reading snapshot.
int64[] added
int64[] updated
int64[] removed                    # Removed from active scene; tombstone still resolves.
ObjectIdChange[] merged
ObjectIdChange[] split
