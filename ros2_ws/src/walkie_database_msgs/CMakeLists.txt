cmake_minimum_required(VERSION 3.10)
project(walkie_database_msgs)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(geometry_msgs REQUIRED)

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

ament_export_dependencies(rosidl_default_runtime)
ament_package()
