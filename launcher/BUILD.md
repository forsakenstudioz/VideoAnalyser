# Building LectureDigest.exe

The exe is a small Go program (`main.go`) that embeds `lecture_digest.py`,
`gui.py` and `requirements.txt` from the repo root. From this folder:

    mkdir -p app && cp ../lecture_digest.py ../gui.py ../requirements.txt app/
    GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o ../LectureDigest.exe .
