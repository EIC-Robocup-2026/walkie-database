# 9. Add the People and Session Services

**Goal:** let walkie-agent-ros remember a guest, recognise them later by
face or by clothes, and forget them when the task ends.

## Background

walkie-agent-ros keeps the people it meets in memory, so a restart of the
agent forgets them. These services keep them in the database file instead.
A session owns the people met in it
([ADR-0010](../architecture/decisions/0010-session-owns-people-and-conversations.md)),
and at most one session is open
([ADR-0017](../architecture/decisions/0017-one-open-session.md)).
[The node's interface](../architecture/database.md#the-nodes-interface)
describes every field.

## Step 1: The message and service files

```text
ros2_ws/src/walkie_database_msgs/
├── msg/
│   ├── Fact.msg
│   ├── ModelInfo.msg
│   ├── ModelVector.msg
│   ├── PersonMatch.msg
│   ├── PersonMatches.msg
│   └── RememberedObject.msg
└── srv/
    ├── EndSession.srv
    ├── FindObjects.srv
    ├── MatchPeople.srv
    ├── RememberPerson.srv
    └── StartSession.srv
```

`CMakeLists.txt` lists the new files next to the old ones:

```cmake title="ros2_ws/src/walkie_database_msgs/CMakeLists.txt (part)"
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Fact.msg"
  "msg/ModelInfo.msg"
  "msg/ModelVector.msg"
  "msg/PersonMatch.msg"
  "msg/PersonMatches.msg"
  "msg/RememberedObject.msg"
  "srv/EndSession.srv"
  "srv/FindObjects.srv"
  "srv/MatchPeople.srv"
  "srv/RememberPerson.srv"
  "srv/StartSession.srv"
  DEPENDENCIES builtin_interfaces geometry_msgs
)
```

A type's hash covers only its own fields and the types it uses. So new
types keep the hashes of `FindObjects` and `RememberedObject`, and a caller
built before this step still gets answers from `find_objects`. A caller
that wants the new services bumps its pin, as in
[Changing a message or service](08-message-package.md#changing-a-message-or-service).

`match_faces` and `match_appearances` share one type, `MatchPeople`,
because they take the same request and give the same response.
`StartSession` returns the models the node takes, so that a caller can
leave out vectors of other models before it sends them.

## Step 2: Migration 0003

`0003_one_open_session_and_unique_ids.sql` allows at most one open session,
and keeps session and person ids from being given twice.
[Migration 0003](04-schema-and-migrations.md#migration-0003-one-open-session)
explains both.

## Step 3: The SQL, in `people.py`

`src/walkie_database/people.py` holds the SQL behind the five services, and
the checks on every request. It knows nothing about ROS, so its tests,
`tests/test_people.py`, call it on a plain connection. `node.py` turns each
request into a call to it, and its answer into a response.

`people.py` raises `Refused` when a request is wrong. The node then puts
the reason in `message` as it is. Any other error is a database failure,
and the node's `message` starts with `The database could not`. Both log a
line, a warning for a refusal and an error for a failure.

Each match query computes the distance to every sample of its model, then
keeps each person's nearest
([ADR-0018](../architecture/decisions/0018-match-people-by-exact-scan.md)):

```sql
WITH sample AS MATERIALIZED (
    SELECT appearance_id, vec_distance_cosine(embedding, :query) AS distance
    FROM "appearance_osnet_x1_0_msmt17"
    WHERE session_id = :session
)
SELECT p.id AS person_id, p.session_id, p.name, p.attributes,
       min(s.distance) AS distance
FROM sample AS s
CROSS JOIN appearance AS a ON a.id = s.appearance_id
CROSS JOIN person AS p ON p.id = a.person_id
WHERE s.distance IS NOT NULL AND a.session_id = :session
GROUP BY p.id
ORDER BY distance, p.id
LIMIT :limit
```

| Part                 | Why                                                                                         |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `MATERIALIZED`       | Computes the distances once, into a temporary table, before any join reads them.           |
| `CROSS JOIN`         | In SQLite, a `CROSS JOIN` keeps the tables in the order they are written. With a plain `JOIN`, SQLite read the scanned samples once per appearance: 1.1 s instead of 54 ms at 5,000 samples. |
| `s.distance IS NOT NULL` | A stored vector of all zeros has no direction, and its distance is NULL.                |
| `:limit`             | `max_results` plus one. The extra row only shows whether people were left out.              |

`test_match_appearances_scans_the_samples_first` checks the plan with
`EXPLAIN QUERY PLAN`, so a change that undoes this fails a test.

The table name comes from the request's `model`. The node puts it in the
SQL only after it has found a `vec0` table of that name in `sqlite_master`,
so a model name that is not a table name never reaches the SQL
(`test_a_model_name_never_reaches_the_sql`).

## Step 4: Test it

```bash
pixi run build-msgs
pixi run test
```

| File                   | What it checks                                                          |
| ---------------------- | ----------------------------------------------------------------------- |
| `tests/test_people.py` | Sessions, ids, every limit and refusal, and the matches, without ROS    |
| `tests/test_node.py`   | The service names and types, the messages, a database failure, and a node restart |
| `tests/test_schema.py` | Migration 0003's index and triggers                                     |

The node tests call the handlers directly, as for `find_objects` in
[step 8](08-message-package.md#step-4-answer-find_objects).
[Getting Started](../getting-started.md#forget-a-session) shows
`end_session` and `start_session` over ROS 2 on the sample house. The
vector services take 512 numbers per vector, so code calls them, not the
command line.
