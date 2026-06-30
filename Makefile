DOCKER_IMAGE ?= nudgis-client:latest
DOCKER_RUN ?= docker run --rm --user "$(shell id -u):$(shell id -g)" -v ${CURDIR}:/opt/src

git_reset:
	git fetch --all
	git reset --hard origin/$(shell git branch --show-current)

build:
	docker build -t ${DOCKER_IMAGE} ${BUILD_ARGS} .

rebuild:BUILD_ARGS = --no-cache
rebuild:build

lint:
	${DOCKER_RUN} -e "RUFF_ARGS=${RUFF_ARGS}" ${DOCKER_IMAGE} make lint_local

lint_local:
	ruff check ${RUFF_ARGS}

deadcode:
	${DOCKER_RUN} ${DOCKER_IMAGE} make deadcode_local

deadcode_local:
	vulture --exclude .eggs --min-confidence 90 .

network_create:
	@docker network create ubicast_network > /dev/null 2>&1 || true

shell:network_create
	${DOCKER_RUN} -it --network ubicast_network --network-alias=nudgis-client ${DOCKER_IMAGE} /bin/sh

test:
	${DOCKER_RUN} -e "PYTEST_ARGS=${PYTEST_ARGS}" ${DOCKER_IMAGE} make test_local

test_local:PYTEST_ARGS := $(or ${PYTEST_ARGS},--cov --no-cov-on-fail --junitxml=report.xml --cov-report xml --cov-report term --cov-report html)
test_local:
	pytest ${PYTEST_ARGS}

publish:
	make clean
	@mkdir -p .local
	${DOCKER_RUN} \
		-e "TWINE_USERNAME=${TWINE_USERNAME}" \
		-e "TWINE_PASSWORD=${TWINE_PASSWORD}" \
		-v ${PWD}/.local:/.local \
		${DOCKER_IMAGE} make publish_local
	@rm -rf .local

publish_local:
	test -z "${TWINE_USERNAME}" \
		&& $echo 'You have to define a value for "TWINE_USERNAME" in your environment' \
		&& exit 1 || true
	test -z "${TWINE_PASSWORD}" \
		&& echo 'You have to define a value for "TWINE_PASSWORD" in your environment' \
		&& exit 1 || true
	pip install build twine
	python -m build
	python -m twine upload --repository pypi -u "${TWINE_USERNAME}" -p "${TWINE_PASSWORD}" dist/*

clean:
	rm -rf .coverage .pytest_cache .local .eggs build dist *.egg-info
	find . -type f -name *.pyc -delete
	find . -type d -name __pycache__ -delete
