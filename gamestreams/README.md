## Add twitch API Key
!set api twitch client_id <********> client_secret <********>

## Show current game alerts
!gamestream twitch alerts

## Search twitch for game
!gamestream twitch search "<GAME NAME>"

## Add game to streams list
!gamestream twitch alert ⁠<CHANNEL> "<GAME NAME>"

## Configure filters for an alert rule
!gamestream twitch alert #STREAMS "<GAME NAME>" language=English
!gamestream twitch alert #STREAMS "<GAME NAME>" title=<FILTER WORDS>
!gamestream twitch alert #STREAMS "<GAME NAME>" language=English title=<FILTER WORDS>