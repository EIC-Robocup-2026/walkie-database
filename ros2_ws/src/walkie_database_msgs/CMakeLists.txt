cmake_minimum_required(VERSION 3.10)
project(walkie_database_msgs)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(walkie_vision_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Event.msg"
  "msg/Fact.msg"
  "msg/LocalizedDetection.msg"
  "msg/ModelInfo.msg"
  "msg/ModelVector.msg"
  "msg/ObjectIdChange.msg"
  "msg/ObjectMatch.msg"
  "msg/ObjectQuery.msg"
  "msg/ObjectSummary.msg"
  "msg/ObjectUpdates.msg"
  "msg/OperationStatus.msg"
  "msg/PersonMatch.msg"
  "msg/PersonMatches.msg"
  "msg/RememberedObject.msg"
  "msg/RoomSummary.msg"
  "msg/SearchPrior.msg"
  "msg/Surface.msg"
  "srv/ArenaReset.srv"
  "srv/DescribeObject.srv"
  "srv/EndSession.srv"
  "srv/FindEvents.srv"
  "srv/FindNear.srv"
  "srv/FindObjects.srv"
  "srv/GetObject.srv"
  "srv/GetRoomContents.srv"
  "srv/GetStoreStatus.srv"
  "srv/ListRooms.srv"
  "srv/LocalizeObjects.srv"
  "srv/MatchPeople.srv"
  "srv/RememberEvent.srv"
  "srv/RememberPerson.srv"
  "srv/ReportSighting.srv"
  "srv/SearchObjects.srv"
  "srv/SetObjectState.srv"
  "srv/StartSession.srv"
  "srv/SubmitDetections.srv"
  DEPENDENCIES builtin_interfaces geometry_msgs sensor_msgs std_msgs walkie_vision_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
