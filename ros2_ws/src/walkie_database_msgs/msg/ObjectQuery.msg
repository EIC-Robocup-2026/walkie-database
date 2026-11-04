string map_id                      # Empty selects node map; a different map is WRONG_MAP.
string room                        # Exact canonical room name, empty means any.
string[] states                    # Empty defaults to present,held.
string layer                       # Empty, anchor or movable (maps movable? in query_objects).
string thing                       # Label/alias/official name; empty means any.
string category                    # Canonical category, empty means any.
Fact[] attributes                  # AND of requested fixed-vocabulary values; unknown is distinct.
string relation                    # Empty,on,inside,near,in_room.
int64 reference_id                 # Related anchor/object; 0 if reference_key/room is used.
string reference_key               # Anchor or surface key; mutually exclusive with reference_id.
float32 near_distance_m 0.6         # Finite >0; used for near only.
# Empty,largest,smallest,heaviest,lightest,thickest,thinnest,nearest,farthest,leftmost,rightmost.
string superlative
# For directional/distance ranking: server looks up this allow-listed frame
# in map at its query time. Default base_footprint. No caller-supplied pose.
string reference_frame "base_footprint"
