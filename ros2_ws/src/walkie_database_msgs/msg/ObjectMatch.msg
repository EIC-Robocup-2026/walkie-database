ObjectSummary object
float32 score                      # Bounded [0,1]; meaningful only within this response.
bool has_similarity
float32 similarity                 # Cosine [-1,1]; 0 if absent.
string[] matched_attributes
string[] unknown_attributes
string[] mismatched_attributes
bool measure_unknown               # Requested superlative cannot measure this object.
