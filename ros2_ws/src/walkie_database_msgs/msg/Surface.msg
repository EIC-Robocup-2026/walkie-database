string key                         # Unique per map, e.g. shelf_2.
int64 anchor_id
string map_id
float64 z                          # Horizontal support plane height in map, metres.
geometry_msgs/Polygon outline      # Map XY, all point.z equals z; no repeated final vertex on wire.
bool geometry_valid
