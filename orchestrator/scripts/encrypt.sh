#!/bin/sh

read TEXT

RESULT=$(echo $TEXT | ansible-vault encrypt_string)

echo "${RESULT}"
