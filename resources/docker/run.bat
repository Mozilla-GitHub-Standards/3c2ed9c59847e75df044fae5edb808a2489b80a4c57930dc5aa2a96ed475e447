
REM RUN FROM ROOT Bugzilla-ETL DIRECTORY, eg ./resources/docker/build.sh
docker run --env-file ./resources/docker/activedata.env -p 8000:8000/tcp --mount source=activedata_state,destination=/app/logs mozilla/activedata:v2.3rc25
