@echo off
for /f "tokens=*" %%i in ('git rev-parse --short HEAD 2^>nul') do set GIT_HASH=%%i
if "%GIT_HASH%"=="" set GIT_HASH=local
echo Building hours-log:%GIT_HASH%
docker build --build-arg GIT_HASH=%GIT_HASH% -t hours-log:local .
echo Done. Run: docker-compose up -d
