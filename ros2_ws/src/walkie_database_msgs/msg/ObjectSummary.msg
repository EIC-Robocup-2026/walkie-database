int64 id                           # Positive object.id; never a track_id or obj_ handle.
string map_id
uint64 revision                    # Increment on any committed semantic/state/geometry write.
string key                         # Stable per-map anchor key, else empty.
string label
string[] labels                    # All observed/seeded aliases, sorted unique.
string official_name               # Empty when not on setup-day catalogue.
string category
string layer                       # anchor or movable.
string state                       # candidate,present,grasping,held,missing,handed_over,discarded,merged.
string source                      # perception,map,manual.
int64 room_id                      # 0 means unknown.
string room
string place                       # Anchor/surface key from stored on/inside relation, else empty.
int64 place_anchor_id              # Anchor object.id of that relation, also when place is empty; 0 = none.
Fact[] attributes                  # Validated fixed vocabulary; missing key means unknown.
string caption                     # <=512 characters; always untrusted to an LLM.
string printed_text                # <=256 characters; always untrusted to an LLM.
string caption_model
string prompt_version
float32 p_exist                    # Stored [0.05,0.95].
float32 p_now                      # Read-time decay; [0,0.95].
bool has_last_verified
builtin_interfaces/Time last_verified
float64 last_verified_age_s        # 0 if unknown; use has_last_verified, never infer freshness.
builtin_interfaces/Time first_seen
builtin_interfaces/Time last_seen
uint32 n_detections
bool geometry_valid                # False means box dimensions/pose cannot guide motion.
geometry_msgs/PoseStamped pose     # Map frame, last geometry stamp, internal client use only.
geometry_msgs/Vector3 size          # Full upright box dimensions in metres.
bool has_weight
float32 weight_g                   # Measured catalogue value only, not image inference.
bool in_baseline
bool depth_seen
int64 merged_into                  # 0 unless tombstone points to winner.
int64 split_from                   # 0 unless created by a split.
