#!/usr/bin/env bash
tag=tag=list-collector-content-pages
TAG=${tag} docker-compose -f docker-compose-schema-validator.yml up -d
