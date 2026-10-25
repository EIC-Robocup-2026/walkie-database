# One embedding, and the model that made it. The database runs no models:
# the caller makes the vector (walkie-database ADR-0006).

# The <model> part of the vector table's name, for example arcface_w600k_r50
# for face_arcface_w600k_r50. Vectors of two models are never compared.
# StartSession's response lists the models the node takes.
string model
# The vector. It must have as many values as the model's table takes, 512
# for both people tables. Its length as a vector does not matter: the node
# scales it to length 1, which keeps every cosine distance. It must not be
# all zeros, and it must not hold NaN or infinity. Named like every other
# embedding field, so that a filter on fields named *embedding catches it.
float32[] embedding
