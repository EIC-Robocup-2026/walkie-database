uint8 OK=0
uint8 INVALID=1
uint8 NOT_FOUND=2
uint8 STALE=3
uint8 TF_UNAVAILABLE=4
uint8 WRONG_MAP=5
uint8 UNSUPPORTED=6
uint8 BUSY=7
uint8 NOT_READY=8
uint8 INTERNAL=9
uint8 CONFLICT=10
uint8 code                         # Always assigned explicitly by the server.
string message                     # Human-readable reason, <=512 characters.
