# One object in Walkie's memory: a row of walkie-database's object table.

# object.id. Stays the same while the object is remembered.
int64 id
# What the detector called it, for example "cup".
string label
# A short description, for example "a red mug with a handle". Empty when
# there is none.
string caption

# The map the object belongs to (map.id), and that map's TF frame
# (map.frame_id). The box below is in this frame, in metres.
string map_id
string frame_id
# The object's bounding box: its centre, its rotation from the frame's axes,
# and its full side lengths along its own axes.
geometry_msgs/Point position
geometry_msgs/Quaternion orientation
geometry_msgs/Vector3 size

# The name of the room the object is in. Empty when it is in no room.
string room

# How many detections were merged into it. 0 for an object that was never
# detected, such as furniture drawn on the map.
uint32 n_detections
# When the object was last detected, or added if it never was.
builtin_interfaces/Time last_seen
