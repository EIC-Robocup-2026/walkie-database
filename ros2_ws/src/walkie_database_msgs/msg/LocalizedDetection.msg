uint32 detection_index             # Original request array index, including failed entries.
OperationStatus status
geometry_msgs/PointStamped centroid # Exact observation stamp, node's map frame.
uint32 points_used                 # Valid transformed input points; no fused/history points.
