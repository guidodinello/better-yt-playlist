# Privacy Policy

better-yt-playlist is a personal command-line tool, run by its author on their own
machines. It is not a hosted service and has no other users.

- **What it accesses:** with your Google authorization, your YouTube playlists and their
  videos' public metadata (titles, channels, durations), via the YouTube Data API. It can
  reorder, add and remove items in playlists you own when you run a command that does so.
- **Where data goes:** everything is stored in a local SQLite file on the machine running
  the tool. OAuth tokens are stored locally in `token.json`. Song artist/title pairs are sent
  to the Last.fm API to look up similar songs. Nothing is sent anywhere else, sold, or shared.
- **Revoking access:** remove the app at <https://myaccount.google.com/permissions> and
  delete the local database and `token.json`.

Contact: guido.dinello@gmail.com
