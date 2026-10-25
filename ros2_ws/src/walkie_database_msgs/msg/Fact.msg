# One thing a person told the robot, for example key "drink" and value
# "coke". The facts of a person are stored in person.attributes, a JSON
# object.

# Not empty, at most 64 characters. Spaces at either end are ignored.
string key
# Not empty, at most 256 characters. Spaces at either end are ignored. A
# value stored in JSON as something other than a string, such as true, comes
# back as its JSON text, "true".
string value
