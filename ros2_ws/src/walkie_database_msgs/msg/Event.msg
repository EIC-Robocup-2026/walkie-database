int64 id
builtin_interfaces/Time stamp      # Node clock at commit; not caller clock.
string kind                        # action,note,error,handover initially.
string text                        # <=512 characters, no personal details.
string map_id
bool has_robot_pose
geometry_msgs/PoseStamped robot_pose # TF at stamp; zero/empty if unavailable, never latest fallback.
int64 room_id                      # 0 unknown.
int64 object_id                    # 0 none.
int64 person_id                    # 0 none; tools never expose person records.
int64 session_id                   # 0 none.
string task_id
string cause
