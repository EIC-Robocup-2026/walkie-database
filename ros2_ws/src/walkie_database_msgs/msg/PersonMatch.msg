# One stored person near a query, with the distance of their nearest sample.

# person.id. The node never gives the same id to two people, also after a
# person is forgotten.
int64 person_id
# The session that met them, person.session_id. 0 for a person kept across
# sessions, such as a team member. Kept people are face candidates in every
# session, so a caller that wants only guests leaves out the 0s.
int64 session_id
# Empty while the robot does not know it.
string name
# person.attributes, in the order they were stored.
Fact[] facts
# The cosine distance from the query to this person's nearest sample of the
# query's model: 0 is the same direction, 1 is unrelated, 2 is opposite.
# The node never decides whether a query is a stranger. The caller applies
# its own limit for each model.
float32 distance
