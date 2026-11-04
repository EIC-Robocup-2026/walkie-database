int64 id
string map_id
string name                        # Canonical display name shared with agent places.
string description
int32 floor
geometry_msgs/Polygon outline      # Map XY, z=0.
bool has_stand_pose
geometry_msgs/PoseStamped stand_pose # Map frame; internal navigation use, not free LLM coordinates.
