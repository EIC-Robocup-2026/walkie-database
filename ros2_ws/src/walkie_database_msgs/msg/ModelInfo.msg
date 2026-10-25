# A model whose vectors the database stores and searches: one vector table.

# The <model> part of the table's name, as in ModelVector.model, for example
# arcface_w600k_r50 for face_arcface_w600k_r50.
string model
# How many values each vector of this model has, for example 512.
uint32 length
