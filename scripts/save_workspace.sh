#! /bin/bash
COMPOSE_PROJECT_NAME=`cat .env | grep COMPOSE_PROJECT_NAME | cut -d '=' -f 2`
rm -rf lab/configs/jupyter_configs
docker cp ${COMPOSE_PROJECT_NAME}_lab_1:/root/.jupyter lab/configs/jupyter_configs
