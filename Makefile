RAP_URI = 10.16.3.1
EGG_BASENAME = ${SERVICE}.egg
PYTHON_FILES = $(shell find py -name "*.py")
PYTHON_FILES_PATH_FROM_PY_ROOT = $(shell find py -name "*.py" | cut -d "/" -f 2-)
SERVICES_DEPLOYMENT_PATH = $(shell python get_system_setting.py serviceFilesDirPath)
UPSETO_REQUIREMENTS_FULFILLED = $(shell upseto checkRequirements 2> /dev/null; echo $$?)

all: check_convention build

check_convention:
	pep8 py --max-line-length=109

COVERED_FILES=py/rackattack/stats/main_allocation_stats.py,py/rackattack/stats/tests/insert_some_records.py
unittest: validate_requirements
	UPSETO_JOIN_PYTHON_NAMESPACES=Yes PYTHONPATH=py python -m coverage run -m rackattack.stats.tests.insert_some_records
	python -m coverage report --show-missing --fail-under=10 --include=$(COVERED_FILES)

run_with_mocked_db:
	UPSETO_JOIN_PYTHON_NAMESPACES=yes PYTHONPATH=py RACKATTACK_PROVIDER=tcp://rackattack-provider.dc1.strato:1014@@amqp://guest:guest@rackattack-provider.dc1.strato:1013@@http://rackattack-provider.dc1.strato:1016 python py/rackattack/stats/tests/run_with_mocked_db.py

.PHONY: build
build: validate_requirements build/$(EGG_BASENAME)

build/${EGG_BASENAME}: validate_requirements ${PYTHON_FILES}
	mkdir -p $(@D)
	cd py; python -m upseto.packegg --entryPoint ${PYTHON_FILES_PATH_FROM_PY_ROOT} --output=../$@ --createDeps=../$@.dep --compile_pyc --joinPythonNamespaces

-include build/$(EGG_BASENAME).dep

.PHONY: install
install:
	make install_service SERVICE=rackattack-hosts-stats
	make install_service SERVICE=rackattack-allocation-stats

.PHONY: install_service
install_service: validate_requirements clean build/$(EGG_BASENAME)
	-sudo systemctl stop ${SERVICE}.service
	-sudo mkdir /usr/share/${SERVICE}
	sudo cp build/$(EGG_BASENAME) /usr/share/${SERVICE}
	sudo sh -c "sed 's/<RAP_URI>/${RAP_URI}/g' ${SERVICE}.service | sed 's/<PYTHONPATH>/\/usr\/share\/$(SERVICE)\/$(EGG_BASENAME)/g' > '${SERVICES_DEPLOYMENT_PATH}/${SERVICE}.service'"
	sudo systemctl enable ${SERVICE}

uninstall:
	-sudo systemctl stop $(SERVICE).service
	-sudo systemctl disable $(SERVICE).service
	-sudo rm -fr ${SERVICES_DEPLOYMENT_PATH}/$(SERVICE).service
	sudo rm -fr /usr/share/$(SERVICE)

.PHONY: validate_requirements
UPSETO_REQUIREMENTS_FULFILLED = $(shell upseto checkRequirements 2> /dev/null; echo $$?)
validate_requirements:
ifneq ($(SKIP_REQUIREMENTS),1)
	sudo pip install -r requirements.txt
	sudo pip install -r ../rackattack-api/requirements.txt
ifeq ($(UPSETO_REQUIREMENTS_FULFILLED),1)
	$(error Upseto requirements not fulfilled. Run with SKIP_REQUIREMENTS=1 to skip requirements validation.)
	exit 1
endif
endif

clean:
	-rm -rf build
