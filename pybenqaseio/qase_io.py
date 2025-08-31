import json
import os
import re
from glob import glob
from typing import Union, Dict, List

from qase.api_client_v1 import Configuration, ResultCreate, ApiClient, ResultsApi, ApiException, SearchApi, RunsApi, \
    RunCreate, CasesApi, TestCaseUpdate, TestCaseCreate, MilestonesApi, MilestoneCreate, PlansApi, SuitesApi, \
    SuiteCreate, SuiteUpdate

from pybenutils.cli_tools import cli_main_for_class
from pybenutils.tests_files_utils import get_pytest_files_index
from pybenutils.utils_logger.config_logger import get_logger

logger = get_logger()


class QaseIO:
    def __init__(self, project_code: str, qase_token:str, qase_pytest_api_key: str):
        self.project_code = project_code if project_code else os.environ.get('QASE_PROJECT_CODE', '')
        self.qase_pytest_api_key = qase_pytest_api_key if qase_pytest_api_key else os.environ.get('QASE_PYTEST_API_KEY', '')
        self.configuration = Configuration(host='https://api.qase.io/v1')
        self.configuration.api_key['TokenAuth'] = qase_token if qase_token else os.environ.get('QASE_TOKEN', '')

    class QaseConfig:
        config_file_name = 'qase.config.json'

        def __init__(self, project_code: str, qase_pytest_api_key: str):
            self.project_code = project_code
            self.qase_pytest_api_key = qase_pytest_api_key

            qase_default_config = {
                "mode": "testops",
                "fallback": "report",
                "report": {
                    "driver": "local",
                    "connection": {
                        "local": {
                            "path": "./build/qase-report",
                            "format": "json"
                        }
                    }
                },
                "testops": {
                    "project": self.project_code,
                    "api": {
                        "token": self.qase_pytest_api_key,
                        "host": "qase.io"
                    },
                    "run": {
                        "complete": False
                    },
                    "defect": False,
                    "bulk": True,
                    "chunk": 200
                },
                "framework": {
                    "pytest": {
                        "capture": {
                            "logs": True,
                            "http": True
                        }
                    }
                },
                "environment": "local"
            }

            if os.path.exists('qase.config.json'):
                self.read()
            else:
                self.config_obj = qase_default_config

        def write(self):
            with open(self.config_file_name, 'w') as f:
                json.dump(self.config_obj, f)

        def read(self):
            with open(self.config_file_name, 'r') as f:
                self.config_obj = json.load(f)

        def remove(self):
            full_path = os.path.join(os.getcwd(), self.config_file_name)
            if os.path.exists(full_path):
                os.remove(full_path)

        def add_run_id(self, run_id: Union[int, str]):
            self.config_obj['testops']['run']['id'] = int(run_id)
            self.config_obj['testops']['run'].pop('title', '')
            self.config_obj['testops']['run']['complete'] = False
            self.write()

        def add_run_title(self, run_title: str):
            self.config_obj['testops']['run']['title'] = run_title
            self.config_obj['testops']['run'].pop('id', 0)
            self.config_obj['testops']['run']['complete'] = True
            self.write()

        def complete_run(self):
            self.config_obj['testops']['run']['complete'] = True
            self.write()

        def dont_complete_run(self):
            self.config_obj['testops']['run']['complete'] = False
            self.write()

    def get_all_instances(self, func, code, limit=100, offset=0, *args, **kwargs):
        res = func(code=code, limit=limit, offset=offset, *args, **kwargs)
        if hasattr(res, 'result'):
            if limit + offset < res.result.filtered:
                return [i for i in res.result.entities] + self.get_all_instances(
                    func, code=code, limit=limit, offset=offset + limit, *args, **kwargs)
        return [i for i in res.result.entities]

    def get_test_case(self, case_id:int):
        """Returns the test case details

        :param case_id: An int representing the Qase test case id taken from the test case name.
        :return: The Qase API result dict with a 'status' key containing a boolean.
        """
        assert case_id, 'case_id is required'
        with ApiClient(self.configuration) as api_client:
            api_instance = CasesApi(api_client)
            try:
                return api_instance.get_case(self.project_code, case_id)
            except ApiException as ex:
                logger.error(f'Exception while trying to get Qase test case "{case_id}": {ex}')

    def get_plans(self, **kwargs):
        """Return a list of Qase plans"""
        with ApiClient(self.configuration) as api_client:
            api_instance = PlansApi(api_client)
            return self.get_all_instances(api_instance.get_plans, self.project_code, **kwargs)

    def get_runs(self, **kwargs):
        """Returns a list of Qase runs"""
        with ApiClient(self.configuration) as api_client:
            api_instance = RunsApi(api_client)
            return self.get_all_instances(api_instance.get_runs, self.project_code, **kwargs)

    def get_suites(self, **kwargs):
        """Return a list of Qase plans"""
        with ApiClient(self.configuration) as api_client:
            api_instance = SuitesApi(api_client)
            return self.get_all_instances(api_instance.get_suites, self.project_code, **kwargs)

    def get_cases(self, **kwargs):
        """Return a list of Qase plans"""
        with ApiClient(self.configuration) as api_client:
            api_instance = CasesApi(api_client)
            return self.get_all_instances(api_instance.get_cases, self.project_code, **kwargs)

    def update_test_suites_from_pytest(self,
                                       move_cases=False,
                                       root_parent_suite: int = None,
                                       known_suites: Dict = None,
                                       assert_parent_suite = False):
        """Update the test suites in Qase from the pytest file structure.
        pytest names are constructed "test_<parent suite>_<suite name>"

        :param move_cases: Move existing cases to the new suites
        :param root_parent_suite: Override this to place all new parent suites inside this one
        :param known_suites: Override this with a dict of known suites to override an exact match with a suite name.
         Example: {'client': 1} a file named test_client_test will be placed in suite 1 regardless of the suite name

        :param assert_parent_suite: Ensure the parent suite can only be selected from the known_suites suites

        :return:
        """
        if not known_suites:
            known_suites = {}
        full_index = get_pytest_files_index(os.path.abspath('.'))
        qase_suites = self.get_suites(project_code=self.project_code)
        qase_cases = self.get_cases(project_code=self.project_code)
        qase_new_dict = {}
        for test_file in full_index:
            name_split = test_file.get('Suite Name', '').split()
            if name_split:
                parent_suite = name_split[1]
                suite_name = ' '.join(name_split[1:])
                if parent_suite not in qase_new_dict:
                    if assert_parent_suite:
                        assert parent_suite in known_suites, f'Parent suite "{parent_suite}" not found in known suites'
                    else:
                        suite_id = 0
                        for suite in qase_suites:
                            clean_suite_name = ' '.join(
                                suite.title.lower().split('(', 1)[0].strip().replace('-', '').split())
                            if clean_suite_name == parent_suite.lower():
                                suite_id = suite.id
                                break
                        if not suite_id:
                            suite_id = self.create_qase_suite(suite_name=parent_suite,
                                                              project_code=self.project_code,
                                                              parent_id=root_parent_suite)
                            logger.info(f'New suite created "{suite_name}" {suite_id=}')
                            qase_suites = self.get_suites()
                            known_suites[parent_suite] = suite_id
                    parent_suite_id = known_suites[parent_suite]
                    qase_new_dict[parent_suite] = {'id': parent_suite_id}
                if suite_name not in qase_new_dict[parent_suite]:
                    suite_id = 0
                    if suite_name in known_suites:
                        suite_id = known_suites[suite_name]
                    else:
                        for suite in qase_suites:
                            clean_suite_name = ' '.join(
                                suite.title.lower().split('(', 1)[0].strip().replace('-', '').split())
                            if clean_suite_name == suite_name.lower():
                                suite_id = suite.id
                                break
                    if not suite_id:
                        suite_id = self.create_qase_suite(suite_name=suite_name, project_code=self.project_code,
                                                          parent_id=qase_new_dict[parent_suite].get('id', root_parent_suite))
                        logger.info(f'New suite created "{suite_name}" {suite_id=}')
                        qase_suites = self.get_suites()
                    qase_new_dict[parent_suite][suite_name] = {'cases': [], 'id': suite_id}
                for test_case in test_file['Test Cases']:
                    qase_id = test_case.get('Qase ID', '')
                    if qase_id:
                        qase_new_dict[parent_suite][suite_name]['cases'].append(qase_id)
                        for qase_case in qase_cases:
                            if qase_case.id == qase_id:
                                if qase_case.suite_id != qase_new_dict[parent_suite][suite_name]['id']:
                                    if move_cases:
                                        logger.info(f'Moving Test case {qase_id} to Qase suite "{suite_name}"')
                                        self.update_a_test_case(
                                            case_id=qase_id,
                                            update_dict={'suite_id': qase_new_dict[parent_suite][suite_name]['id']}
                                        )
                                    else:
                                        logger.warning(
                                            f'Test case {qase_id} needs to be moved to Qase suite "{suite_name}"')
                                break

        return qase_new_dict




    def update_test_results(self, run_id: int, case_id: int, status: str, comment: str = '',
                            os_name: str = '', trans_mode: str = '', test_params: str = '', test_time: int = 1):
        """Updates a test result in a specific Qase run. Adds the test case to the run if it's not there.

        :param run_id: An int representing the Qase test run id taken from the test run url.
         e.g. 17 for https://app.qase.io/run/CODE/dashboard/17
        :param case_id: An tint representing the test cast id we're reporting the results for. e.g. 71 for CODE-71.
        :param status: A string representing a test result. possible values are: 'passed', 'failed', 'skipped', 'blocked',
        'invalid'
        :param comment: A string to insert to the comment field in the Qase test outcome.
        :param os_name: An optional string to report the test outcome for a specific os configuration. Case-sensitive.
        e.g. 'MacOS - Ventura'
        :param trans_mode: An optional string to report the trans mode for a test result. Case-sensitive.
        e.g. 'Explicit mode (Forced)'
        :param test_params: An optional string to report additional test parameters for a test result. Case-sensitive.
        e.g. 'Browser: Chrome'
        :param test_time: An int representing the test duration in milliseconds.
        :return: The Qase API result dict with 'status' key containing a boolean.
        """
        # Enter a context with an instance of the API client
        with ApiClient(self.configuration) as api_client:
            # Create an instance of the API class
            api_instance = ResultsApi(api_client)

            result_dict = dict(
                case_id=case_id,
                status=status,
                comment=comment,
                time_ms=test_time
            )
            if os_name:
                result_dict['param'] = {'OS': os_name}

            if trans_mode or test_params:
                params_string = f'{f"Transparency mode: {trans_mode}" if trans_mode else ""}' \
                                f'{f"; Other params: {test_params}" if test_params else ""}'
                if 'param' in result_dict:
                    result_dict['param'].update({'Test params': params_string})
                else:
                    result_dict['param'] = {'Test params': params_string}

            result_object = ResultCreate(**result_dict)

            try:
                api_response = api_instance.create_result(self.project_code, run_id, result_object)
                return api_response
            except ApiException as e:
                logger.error(f"Exception while trying to update result: {e}")
                logger.debug(result_dict)

    def create_qase_test_run(self, title: str, custom_fields: Dict = None, **kwargs):
        """
        Create a test run in Qase for the provided project with the title.
        :param title: The title for the test run
        :param custom_fields: An optional dictionary that populates custom fields in the test run in Qase.
        e.g. {
                "3": "5.0.0-cb4e787",
                "1": "https://autoetp2.jenkins.akamai.com/job/CODE-desktop-smoke-test/125/allure/",
            }
        :return: The Qase API result dict with 'status' key containing a boolean.
        """

        args = {
            "title": title,
            "is_autotest": True,
        }
        if custom_fields is not None:
            args["custom_field"] = custom_fields
        args.update({k: v for k, v in kwargs.items() if v})
        with ApiClient(self.configuration) as api_client:
            api_instance = RunsApi(api_client)

            try:
                api_response = api_instance.create_run(self.project_code, RunCreate(**args))
                return api_response
            except ApiException as e:
                logger.error(f"Exception while trying to create a Qase run: {e}")

    def complete_qase_test_run(self, run_id: int):
        """
        Completes a test run in Qase.
        :param run_id: An int representing the Qase test run id taken from the test run url.
        e.g. 17 for https://app.qase.io/run/CODE/dashboard/17
        :return: The Qase API result dict with 'status' key containing a boolean.
        """

        with ApiClient(self. configuration) as api_client:
            api_instance = RunsApi(api_client)

            try:
                logger.info(f'Attempting to complete a Qase test run {run_id} at project {self.project_code}')
                api_response = api_instance.complete_run(self.project_code, run_id)
                return api_response
            except ApiException as e:
                logger.error(f'Exception while trying to complete a Qase run "{run_id}": {e}')

    def delete_a_test_case(self, case_id: int):
        """
        Deletes a test case in Qase.
        :param case_id: An int representing the Qase test case id taken from the test case name.
        e.g. 17 for CODE-17
        :return: The Qase API result dict with 'status' key containing a boolean.
        """

        with ApiClient(self.configuration) as api_client:
            api_instance = CasesApi(api_client)

            try:
                api_response = api_instance.delete_case(self.project_code, case_id)
                return api_response
            except ApiException as e:
                logger.error(f'Exception while trying to delete a Qase test case "{case_id}": {e}')

    def update_a_test_case(self, case_id: int, update_dict: dict):
        """Update a test case in Qase.

        :param case_id: An int representing the Qase test case id taken from the test case name.
        e.g. 17 for CODE-17
        :param update_dict: A dict of params to override in the test case according to the Qase API. e.g.
        {
            "tags": ["sanity", "Pytool"],
            "params": {"os": ["Windows 10", "Windows 11"]},
            "title": "New Title",
            "preconditions": "New preconditions string"
        }
        :return: The Qase API result dict with 'status' key containing a boolean.
        """

        with ApiClient(self.configuration) as api_client:
            api_instance = CasesApi(api_client)

            try:
                api_response = api_instance.update_case(self.project_code, case_id, TestCaseUpdate(**update_dict))
                return api_response
            except ApiException as e:
                logger.error(f'Exception while trying to update a Qase test case "{case_id}": {e}')

    def create_test_case(self, payload_dict: dict):
        """Creates a new test case in Qase with the given payload

        :param payload_dict: A dict of params
        :return: The Qase API result dict with 'status' key containing a boolean
        """
        with ApiClient(self.configuration) as api_client:
            api_instance = CasesApi(api_client)

            try:
                api_response = api_instance.create_case(self.project_code, TestCaseCreate(**payload_dict))
                return api_response
            except ApiException as ex:
                logger.error(f'Exception while trying to create a new Qase test case: {ex}')

    def get_milestone_id(self, milestone_title: str, create_missing=False) -> int:
        """Returns a Qase milestone id (int) if exists

        :param milestone_title: Required milestone title
        :param create_missing: Create new milestone if not found
        :return: Milestone id number
        """
        milestone_id = 0
        with ApiClient(self.configuration) as api_client:
            api_instance = MilestonesApi(api_client)
            res = api_instance.get_milestones(code=self.project_code, limit=100)
            if res:
                matches = [i for i in res.result.entities if milestone_title == i.title]
                if matches:
                    return matches[0].id
        if create_missing:
            milestone_id = self.create_milestone(milestone_title)
        return milestone_id

    def create_milestone(self, milestone_title: str) -> int:
        """Creates a new Qase milestone

        :param milestone_title: Milestone "title"
        :return: New milestone id number
        """
        with ApiClient(self.configuration) as api_client:
            api_instance = MilestonesApi(api_client)
            res = api_instance.create_milestone(self.project_code, MilestoneCreate(title=milestone_title))
            return res.result.id

    def get_run(self, run_id: int = 0):
        """Returns the test case details

        :param run_id: An int representing the Qase run id taken from the run.
        :return: The Qase API result dict with 'status' key containing a boolean.
        """
        with ApiClient(self.configuration) as api_client:
            api_instance = RunsApi(api_client)

            try:
                if run_id:
                    api_response = api_instance.get_run(self.project_code, run_id)
                    return api_response
            except ApiException as ex:
                logger.error(f"Exception while trying to get a Qase run '{run_id}': {ex}")

    def qase_copy_params(self, case_to_copy: int, cases_to_update: List[int]):
        """Copies the parameters of the given Qase case_id to the given test cases to update (Override existing)

        :param case_to_copy: The Qase case id to copy the params section from
        :param cases_to_update: Qase cases to update as a list of integers
        """
        origin = self.get_test_case(case_id=case_to_copy)
        origin_params = origin.result.params.to_dict()
        logger.info(
            f'Updating parameters dict "{origin_params}" from case "{case_to_copy}" to cases: {cases_to_update}')
        for case_to_update in cases_to_update:
            self.update_a_test_case(case_id=case_to_update, update_dict={'params': origin_params})

    def qase_remove_unwanted_params_from_cases(self, cases: List[int], unwanted_params: Dict[str, List[str]]):
        """Removes unwanted parameters from all the given test cases

        :param cases: List of cases to update
        :param unwanted_params: Dict of unwanted params to remove
        """
        for case in cases:
            unwanted_found = False
            origin = self.get_test_case(case_id=case)
            if not origin:
                continue
            origin_params = origin.result.params.to_dict()
            for unwanted_key, unwanted_values in unwanted_params.items():
                if unwanted_key in origin_params:
                    for unwanted_value in unwanted_values:
                        if unwanted_value in origin_params[unwanted_key]:
                            origin_params[unwanted_key].remove(unwanted_value)
                            if not origin_params[unwanted_key]:
                                del origin_params[unwanted_key]
                            unwanted_found = True
            if unwanted_found:
                logger.info(f'Removing {unwanted_params} params from {case}')
                self.update_a_test_case(case_id=case, update_dict={'params': origin_params})

    def qase_add_params_to_cases(self, cases: List[int], params: Dict[str, List[str]]):
        """Adds given parameters to all the given test cases

        :param cases: List of cases to update
        :param params: Dict of parameters to update into cases
        """
        for case in cases:
            origin = self.get_test_case(case_id=case)
            if not origin:
                continue
            origin_params = origin.result.params.to_dict()
            for key, values in params.items():
                if key not in origin_params:
                    origin_params[key] = values
                else:
                    origin_params[key] = list(set(origin_params[key] + values))
            logger.info(f'Updating {params} params to {case}')
            self.update_a_test_case(case_id=case, update_dict={'params': origin_params})

    def replace_params_in_cases(self,
                                cases: List[int],
                                unwanted_params: Dict[str, List[str]],
                                wanted_params: Dict[str, List[str]]):
        """Replaces the given unwanted params with the given wanted params.

         Will only replace places where unwanted params exist

        :param cases: List of cases to update
        :param unwanted_params: Dict of unwanted params to replace
        :param wanted_params: Dict of parameters to update into cases
        """
        for case in cases:
            add_wanted_params = False
            origin = self.get_test_case(case_id=case)
            if not origin:
                continue
            origin_params = origin.result.params.to_dict()
            for unwanted_key, unwanted_values in unwanted_params.items():
                if unwanted_key in origin_params:
                    for unwanted_value in unwanted_values:
                        if unwanted_value in origin_params[unwanted_key]:
                            origin_params[unwanted_key].remove(unwanted_value)
                            if not origin_params[unwanted_key]:
                                del origin_params[unwanted_key]
                            add_wanted_params = True
            if add_wanted_params:
                for key, values in wanted_params.items():
                    if key not in origin_params:
                        origin_params[key] = values
                    else:
                        origin_params[key] = list(set(origin_params[key] + values))
                logger.info(f'Removing {unwanted_params} params from {case}')
                logger.info(f'Updating {wanted_params} params to {case}')
                self.update_a_test_case(case_id=case, update_dict={'params': origin_params})

    def qase_remove_all_params_from_cases(self, cases: List[int]):
        """Removes unwanted parameters from all the given test cases

        :param cases: List of cases to update
        """
        for case in cases:
            origin = self.get_test_case(case_id=case)
            if origin:
                origin_params = origin.result.params.to_dict()
                if origin_params:
                    logger.info(f'Removing all params from {case}')
                    self.update_a_test_case(case_id=case, update_dict={'params': {}})

    def qase_search_query(self, query, limit=100, offset=0, *args, **kwargs):
        with ApiClient(self.configuration) as api_client:
            api_instance = SearchApi(api_client)
            func = api_instance.search
            res = func(query=query, limit=limit, offset=offset, *args, **kwargs)
            if hasattr(res, 'result'):
                if limit + offset < res.result.total:
                    return [i for i in res.result.entities] + self.qase_search_query(
                        query=query, limit=limit, offset=offset + limit, *args, **kwargs)
            return [i.actual_instance for i in res.result.entities]

    def create_qase_suite(self, suite_name, parent_id=None, **kwargs):
        """Create a new Qase suite"""
        args = {
            "title": suite_name,
        }
        if parent_id:
            args['parent_id'] = parent_id
        args.update({k: v for k, v in kwargs.items() if v})
        with ApiClient(self.configuration) as api_client:
            api_instance = SuitesApi(api_client)
            try:
                api_response = api_instance.create_suite(self.project_code, SuiteCreate(**args))
                return api_response.result.id
            except ApiException as e:
                logger.error(f"Exception while trying to create a Qase suite: {e}")

    def update_suite(self, suite_id: int, **kwargs):
        with ApiClient(self.configuration) as api_client:
            api_instance = SuitesApi(api_client)

            try:
                api_response = api_instance.update_suite(self.project_code, suite_id, SuiteUpdate(**kwargs))
                return api_response
            except ApiException as e:
                logger.error(f'Exception while trying to update a Qase suite "{suite_id}": {e}')

def list_all_qase_ids(root_dir='.'):
    """Return a list of all duplicate qase ids from our test files"""
    qase_ids = []
    pattern = 'test_*.py'
    test_files = glob(pattern, root_dir=os.path.abspath(f'{root_dir}'))
    for test_file in test_files:
        test_file_path = os.path.join(root_dir, test_file)
        with open(test_file_path, 'r') as f:
            file_text = f.read()
        qase_ids += re.findall(r'(qase.id\(\d+\))', file_text)
    return qase_ids

if __name__ == '__main__':
    cli_main_for_class(QaseIO)