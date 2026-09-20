# Export to a Google Drive folder

Paste a Drive folder link, tick what to send, and sorted uploads it: ticked categories land in
`categories/<name>/`, saved people in `people/<name>/`, a selection in `selection/`, all inside the
folder you pasted. The client shares a folder with you, you paste its link, done. Nothing on the
shoot disk is touched; a web-size copy, when you ask for one, is made in a temp folder and thrown
away after the upload.

You sign in once. Before that works, Google needs to know the app exists, which is a one-time,
six-step trip through the Google Cloud console. It takes about five minutes.

## One-time setup: the OAuth client file

1. **Make a project.** Open <https://console.cloud.google.com/>, sign in with the Google account
   whose Drive you will upload from, click the project picker at the top and choose **New project**.
   Call it `sorted` (any name works). Wait for it to be created and make sure it is selected.

2. **Turn on the Drive API.** Left menu, **APIs and Services**, then **Library**. Search for
   `Google Drive API`, open it, click **Enable**.

3. **Set up the consent screen.** **APIs and Services**, then **OAuth consent screen** (Google now
   calls this page *Google Auth Platform*, then *Branding* and *Audience*). Pick **External**, give
   the app the name `sorted`, put your own email in the support and developer contact fields, save.
   Under **Audience**, leave the app in **Testing** and add your own Google address as a **test
   user**. Testing mode is fine forever for a one-person app: only the addresses on that list can
   sign in, and there is no review to go through.

4. **Create the client.** **APIs and Services**, then **Credentials**, then **Create credentials**,
   then **OAuth client ID**. Application type: **Desktop app**. Name it `sorted desktop`. Click
   **Create**.

5. **Download the JSON.** In the dialog that follows (or from the download icon next to the client
   in the list), click **Download JSON**. It is a small file named something like
   `client_secret_1234-abcd.apps.googleusercontent.com.json`.

6. **Put it where the app looks.** Rename it to `google_client.json` and move it to
   `~/Library/Application Support/photosort/google_client.json` (the exact path is shown in the
   app next to the sign-in button, and by `GET /api/drive/status` as `client_path`). If the
   `photosort` folder is not there yet, make it.

That file identifies the app, not you. It holds a client id and a client secret; for a Desktop
app Google treats the secret as public, so it is not a password, but keep it out of git all the
same.

## Signing in

In the app: **Sign in to Google** on the export panel. From a terminal: `python -m photosort.cli
drive-signin`. Either way the browser opens Google's consent page. Pick the account you added as a
test user, approve the two permissions, and the tab says you can close it. The app keeps a refresh
token at `~/Library/Application Support/photosort/google_token.json`, readable only by your user
(mode 0600). Access tokens expire every hour and are refreshed on their own; you will not be asked
again unless you sign out, revoke the app at <https://myaccount.google.com/permissions>, or leave
the token unused for six months (Google's rule for apps in Testing mode).

To sign out: the **Sign out** button, `POST /api/drive/signout`, or delete `google_token.json`.
Signing out forgets the token on this Mac; it does not revoke anything at Google, use the
permissions page for that.

## What the app asks for, and why

- `https://www.googleapis.com/auth/drive`: read and write all of your Drive. The narrower
  `drive.file` scope only sees files the app created itself, and the folder your client shared
  with you was never created by this app, so with `drive.file` the app could not even find it,
  let alone upload into it. Full scope is the only one that can write into a folder someone else
  shared. The app only ever creates subfolders and uploads files; it never lists, moves or deletes
  anything of yours.
- `userinfo.email` and `openid`: the address of the account you signed in with, shown next to the
  sign-in button so you can tell which account is about to receive the files. Google adds `openid`
  on its own whenever an email is asked for, so it is requested up front, otherwise the sign-in
  fails with "scope has changed".

## Free space

Before an upload the app checks the space it can see. A file you upload into a My Drive folder is
yours, even when the folder belongs to the client and was only shared with you, so it counts
against your own Google storage, not theirs. The upload is refused when it would not leave 512 MB
of your quota spare; the web-size option is the usual way out, or a bigger plan. A folder on a
Shared Drive (a Workspace organisation's pooled storage) is the one case where your quota is not
the limit; there the app cannot see the limit, so the check passes and any "storage full" errors
show up as per-file failures instead.

## Re-running

Every upload is remembered in `~/Library/Application Support/photosort/drive-uploads/
<shoot>-<folderid>.json`: which local file, at which size and modification time, at which web
size, became which Drive file. Run the same export again and files already there are skipped
after one quick check that they still exist on Drive; files deleted or binned on Drive since, and
files that changed locally, go up again. Failed files are retried on the next run. Pick a different
web size and everything goes up again at that size, since it is a different file.

## Web size

With a web size set (3000 px is a good delivery default), photos are re-encoded to that long edge
at JPEG quality 90, with the orientation baked in and the rest of the EXIF kept, under the same
file name. RAW files and videos are never resized; they go up as they are, or with **skip videos**
ticked videos stay home.

## Command line

```
python -m photosort.cli drive-signin
python -m photosort.cli drive-export <shoot> --link <folder link> --categories beach ocean --include-raw
python -m photosort.cli drive-export <shoot> --link <folder link> --people --names Meera Ravi --web-size 3000
python -m photosort.cli drive-export <shoot> --link <folder link> --ids 12 34 56 --name picks
```

## Trouble

- "put your Google OAuth client file at ...": step 6 above.
- "sign in to Google first": the token file is missing; sign in.
- "Access blocked: sorted has not completed the Google verification process": the account you
  picked is not on the test-user list from step 3, or the consent screen is set to Internal on a
  personal account.
- "no folder with that id; check the link and that it was shared with you": the link is right but
  that account cannot see the folder. Ask the client to share it with the address shown next to
  the sign-in button, with Editor access.
- "that is a document link, paste a folder link": you pasted a Docs, Sheets, Slides or file link.
  Open the folder in Drive and copy the address bar, it looks like
  `https://drive.google.com/drive/folders/1AbC...`.
