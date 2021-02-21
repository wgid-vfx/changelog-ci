import json
import os
import re
import subprocess

import requests


# Regex is taken from https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
# It was modified a little bit to make it a bit less restrictive
DEFAULT_SEMVER_REGEX = r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.?(0|[1-9]\d*)?(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
DEFAULT_PULL_REQUEST_TITLE_REGEX = r"^(?i:release)"
DEFAULT_VERSION_PREFIX = "Version:"
DEFAULT_GROUP_CONFIG = []


class ChangelogCI:
    """The class that generates, commits and/or comments changelog"""

    github_api_url = 'https://api.github.com'

    def __init__(
        self, repository,
        event_path, filename='CHANGELOG.md',
        config_file=None, token=None
    ):
        self.repository = repository
        self.filename = filename
        self.config = self._parse_config(config_file)
        self.token = token

        title, number = self._get_pull_request_title_and_number(event_path)
        self.pull_request_title = title
        self.pull_request_number = number

    @staticmethod
    def _default_config():
        """Default configuration for Changelog CI"""
        return {
            "header_prefix": DEFAULT_VERSION_PREFIX,
            "commit_changelog": True,
            "comment_changelog": False,
            "pull_request_title_regex": DEFAULT_PULL_REQUEST_TITLE_REGEX,
            "version_regex": DEFAULT_SEMVER_REGEX,
            "group_config": DEFAULT_GROUP_CONFIG
        }

    @staticmethod
    def _get_changelog_line(item):
        """Generate each line of changelog"""
        return ("* [#{number}]({url}): {title}\n").format(
            number=item['number'],
            url=item['url'],
            title=item['title']
        )

    @staticmethod
    def _get_pull_request_title_and_number(event_path):
        """Gets pull request title from ``GITHUB_EVENT_PATH``"""
        with open(event_path, 'r') as json_file:
            # This is just a webhook payload available to the Action
            data = json.load(json_file) or {}

        title = data.get("pull_request", {}).get('title', "Testing")
        number = data.get('number', 00)

        return title, number

    def _parse_config(self, config_file):
        """parse the config file if not provided use default config"""
        if config_file:
            try:
                with open(config_file, 'r') as config_json:
                    config = json.load(config_json)
                # parse and validate user provided config file
                parse_config(config)
                return config
            except Exception as e:
                msg = f'Invalid Configuration file, error: {e}'
                _print_output('error', msg)

        msg = 'Using Default Config to parse changelog'
        _print_output('warning', msg)

        # if config file not provided
        # or invalid fall back to default config
        return self._default_config()

    def _validate_pull_request(self):
        """Check if changelog should be generated for this pull request"""
        pattern = re.compile(self.config['pull_request_title_regex'])
        match = pattern.search(self.pull_request_title)

        if match:
            return True

        return

    def _get_version_number(self):
        """Get version number from the pull request title"""
        pattern = re.compile(self.config['version_regex'])
        match = pattern.search(self.pull_request_title)

        if match:
            return match.group()

        return

    def _get_file_mode(self):
        """Gets the mode that the changelog file should be opened in"""
        if os.path.exists(self.filename):
            # if the changelog file exists
            # opens it in read-write mode
            file_mode = 'r+'
        else:
            # if the changelog file does not exists
            # opens it in read-write mode
            # but creates the file first also
            file_mode = 'w+'
        return file_mode

    def _get_request_headers(self):
        """Get headers for GitHub API request"""
        headers = {
            'Accept': 'application/vnd.github.v3+json'
        }
        # if the user adds ``GITHUB_TOKEN`` add it to API Request
        # required for ``private`` repositories
        if self.token:
            headers.update({
                'authorization': 'Bearer {token}'.format(token=self.token)
            })

        return headers

    def _get_latest_release_date(self):
        """Using GitHub API gets latest release date"""
        url = (
            '{base_url}/repos/{repo_name}/releases/latest'
        ).format(base_url=self.github_api_url, repo_name=self.repository)

        response = requests.get(url, headers=self._get_request_headers())

        published_date = ''

        if response.status_code == 200:
            response_data = response.json()
            # get the published date of the latest release
            published_date = response_data['published_at']
        else:
            # if there is no previous release API will return 404 Not Found
            msg = (
                f'Could not find any previous release for '
                f'{self.repository}, status code: {response.status_code}'
            )
            _print_output('warning', msg)

        return published_date

    def _get_pull_requests_after_last_release(self):
        """Get all the merged pull request after latest release"""
        previous_release_date = self._get_latest_release_date()

        if previous_release_date:
            merged_date_filter = 'merged:>=' + previous_release_date
        else:
            # if there is no release for the repo then
            # do not filter by merged date
            merged_date_filter = ''

        url = (
            '{base_url}/search/issues'
            '?q=repo:{repo_name}+'
            'is:pr+'
            'is:merged+'
            'sort:author-date-asc+'
            '{merged_date_filter}'
            '&sort=merged'
        ).format(
            base_url=self.github_api_url,
            repo_name=self.repository,
            merged_date_filter=merged_date_filter
        )

        items = []

        response = requests.get(url, headers=self._get_request_headers())

        if response.status_code == 200:
            response_data = response.json()

            # ``total_count`` represents the number of
            # pull requests returned by the API call
            if response_data['total_count'] > 0:
                for item in response_data['items']:
                    data = {
                        'title': item['title'],
                        'number': item['number'],
                        'url': item['html_url'],
                        'labels': [label['name'] for label in item['labels']]
                    }
                    items.append(data)
            else:
                msg = (
                    f'There was no pull request '
                    f'made on {self.repository} after last release.'
                )
                _print_output('error', msg)
        else:
            msg = (
                f'Could not get pull requests for '
                f'{self.repository} from GitHub API. '
                f'response status code: {response.status_code}'
            )
            _print_output('error', msg)

        return items

    def _parse_data(self, version, pull_request_data):
        """Parse the pull requests data and return a writable data structure"""
        string_data = (
            '# ' + self.config['header_prefix'] + ' ' + version + '\n\n'
        )

        group_config = self.config['group_config']

        if group_config:
            for config in group_config:

                if len(pull_request_data) == 0:
                    break

                items_string = ''

                for pull_request in pull_request_data:
                    # check if the pull request label matches with
                    # any label of the config
                    if (
                        any(
                            label in pull_request['labels']
                            for label in config['labels']
                        )
                    ):
                        items_string += self._get_changelog_line(pull_request)
                        # remove the item so that one item
                        # does not match multiple groups
                        pull_request_data.remove(pull_request)

                if items_string:
                    string_data += '\n#### ' + config['title'] + '\n\n'
                    string_data += '\n' + items_string

            if pull_request_data:
                # if they do not match any user provided group
                # Add items in ``Other Changes`` group
                string_data += '\n#### Other Changes\n\n'
                string_data += ''.join(map(self._get_changelog_line, pull_request_data))
        else:
            # If group config does not exist then append it without and groups
            string_data += ''.join(map(self._get_changelog_line, pull_request_data))

        return string_data

    def _commit_changelog(self, string_data):
        """Write changelog to the changelog file"""
        file_mode = self._get_file_mode()

        with open(self.filename, file_mode) as f:
            # read the existing data and store it in a variable
            body = f.read()
            # write at the top of the file
            f.seek(0, 0)
            f.write(string_data)

            if body:
                # re-write the existing data
                f.write('\n\n')
                f.write(body)

        subprocess.run(['git', 'add', self.filename])
        subprocess.run(['git', 'commit', '-m', '(Changelog CI) Added Changelog'])
        subprocess.run(['git', 'push', '-u', 'origin', head_ref])

    def _comment_changelog(self, string_data):
        """Comments Changelog to the pull request"""
        if not self.token:
            # Token is required by the GitHub API to create a Comment
            # if not provided exit with error message
            msg = (
                "Could not add a comment. "
                "``GITHUB_TOKEN`` is required for this operation. "
                "If you want to enable Changelog comment, please add "
                "``GITHUB_TOKEN`` to your workflow yaml file. "
                "Look at Changelog CI's documentation for more information."
            )

            _print_output('error', msg)
            return

        owner, repo = self.repository.split('/')

        payload = {
            'owner': owner,
            'repo': repo,
            'issue_number': self.pull_request_number,
            'body': string_data
        }

        url = (
            '{base_url}/repos/{repo}/issues/{number}/comments'
        ).format(
            base_url=self.github_api_url,
            repo=self.repository,
            number=self.pull_request_number
        )

        response = requests.post(
            url, headers=self._get_request_headers(),
            json=payload
        )

        if response.status_code != 201:
            # API should return 201, otherwise show error message
            msg = (
                f'Error while trying to create a comment. '
                f'GitHub API returned error response for '
                f'{self.repository}, status code: {response.status_code}'
            )

            _print_output('error', msg)

    def run(self):
        """Entrypoint to the Changelog CI"""
        if (
            not self.config['commit_changelog'] and
            not self.config['comment_changelog']
        ):
            # if both commit_changelog and comment_changelog is set to false
            # then exit with warning and don't generate Changelog
            msg = (
                'Skipping Changelog generation as both ``commit_changelog`` '
                'and ``comment_changelog`` is set to False. '
                'If you did not intend to do this please set '
                'one or both of them to True.'
            )
            _print_output('error', msg)
            return

        is_valid_pull_request = self._validate_pull_request()

        if not is_valid_pull_request:
            # if pull request regex doesn't match then exit
            # and don't generate changelog
            msg = (
                f'The title of the pull request did not match. '
                f'Regex tried: "{self.config["pull_request_title_regex"]}", '
                f'Aborting Changelog Generation.'
            )
            _print_output('error', msg)
            return

        version = self._get_version_number()

        if not version:
            # if the pull request title is not valid, exit the method
            # It might happen if the pull request is not meant to be release
            # or the title was not accurate.
            msg = (
                f'Could not find matching version number. '
                f'Regex tried: {self.config["version_regex"]} '
                f'Aborting Changelog Generation'
            )
            _print_output('error', msg)
            return

        pull_request_data = self._get_pull_requests_after_last_release()

        # exit the function if there is not pull request found
        if not pull_request_data:
            return

        string_data = self._parse_data(version, pull_request_data)

        if self.config['commit_changelog']:
            subprocess.run(['echo', '::group::Commit Changelog'])
            self._commit_changelog(string_data)
            subprocess.run(['echo', '::endgroup::'])

        if self.config['comment_changelog']:
            subprocess.run(['echo', '::group::Comment Changelog'])
            self._comment_changelog(string_data)
            subprocess.run(['echo', '::endgroup::'])


def parse_config(config):
    """Parse and Validates user provided config, raises Error if not valid"""
    if not isinstance(config, dict):
        raise TypeError(
            'Configuration does not contain required key, value pairs'
        )

    pull_request_title_regex = config.get('pull_request_title_regex')
    version_regex = config.get('version_regex')

    try:
        # if the regex is not provided or is an empty string
        # just raise KeyError and fallback to default
        if not pull_request_title_regex:
            raise KeyError

        # This will raise an error if the provided regex is not valid
        re.compile(pull_request_title_regex)
    except Exception:
        msg = (
            '``pull_request_title_regex`` was not provided or not valid, '
            'Falling back to default regex.'
        )
        _print_output('warning', msg)
        # if the pull_request_title_regex is not valid or not available
        # fallback to default regex
        config.update({
            "pull_request_title_regex": DEFAULT_PULL_REQUEST_TITLE_REGEX
        })

    try:
        # if the regex is not provided or is an empty string
        # just raise KeyError and fallback to default
        if not version_regex:
            raise KeyError

        # This will raise an error if the provided regex is not valid
        re.compile(version_regex)
    except Exception:
        msg = (
            '``version_regex`` was not provided or not valid, '
            'Falling back to default regex.'
        )
        _print_output('warning', msg)
        # if the version_regex is not valid or not available
        # fallback to default regex
        config.update({
            "version_regex": DEFAULT_SEMVER_REGEX
        })

    try:
        commit_changelog = config['commit_changelog']
        config.update({
            "commit_changelog": bool(commit_changelog)
        })
    except Exception:
        msg = (
            '``commit_changelog`` was not provided or not valid, '
            'falling back to ``True``.'
        )
        _print_output('warning', msg)
        # if commit_changelog is not provided default to True
        config.update({
            "commit_changelog": True
        })

    try:
        comment_changelog = config['comment_changelog']
        config.update({
            "comment_changelog": bool(comment_changelog)
        })
    except Exception:
        msg = (
            '``comment_changelog`` was not provided or not valid, '
            'falling back to ``False``.'
        )
        _print_output('warning', msg)
        # if comment_changelog is not provided default to False
        config.update({
            "comment_changelog": False
        })

    header_prefix = config.get('header_prefix')
    group_config = config.get('group_config')

    if not header_prefix or not isinstance(header_prefix, str):
        msg = (
            '``header_prefix`` was not provided or not valid, '
            'falling back to default prefix.'
        )
        _print_output('warning', msg)
        # if the header_prefix is not not available
        # fallback to default prefix
        config.update({
            "header_prefix": DEFAULT_VERSION_PREFIX
        })

    if not group_config or not isinstance(group_config, list):
        msg = (
            '``group_config`` was not provided or not valid, '
            'falling back to default group config.'
        )
        _print_output('warning', msg)
        # if the group_config is not not available
        # fallback to default group_config
        config.update({
            "group_config": DEFAULT_GROUP_CONFIG
        })
    else:
        try:
            # Check if all the group configs match the schema
            for config in group_config:
                if not isinstance(config, dict):
                    raise TypeError(
                        'group_config items must have key, '
                        'value pairs of title and labels'
                    )
                title = config.get('title')
                labels = config.get('labels')

                if not title:
                    raise KeyError('group_config item must contain title')

                if not labels:
                    raise KeyError('group_config item must contain labels')

                if not isinstance(labels, list):
                    raise TypeError('group_config labels must be an Array')

        except Exception as e:
            msg = (
                f'An error occurred while parsing ``group_config``. Error: {e}'
                f'falling back to default group config.'
            )
            _print_output('warning', msg)
            # Fallback to default group_config
            config.update({
                "group_config": DEFAULT_GROUP_CONFIG
            })


def _print_output(type, message):
    """Helper function to print colorful outputs in GitHub Actions shell"""
    return subprocess.run(['echo', f'::{type}::{message}'])


if __name__ == '__main__':
    # Default environment variable from GihHub
    # https://docs.github.com/en/actions/configuring-and-managing-workflows/using-environment-variables
    event_path = os.environ['GITHUB_EVENT_PATH']
    repository = os.environ['GITHUB_REPOSITORY']
    head_ref = os.environ['GITHUB_HEAD_REF']
    # User inputs from workflow
    filename = os.environ['INPUT_CHANGELOG_FILENAME']
    config_file = os.environ['INPUT_CONFIG_FILE']
    # Token provided from the workflow
    token = os.environ.get('GITHUB_TOKEN')
    # Committer username and email address
    username = os.environ['INPUT_COMMITTER_USERNAME']
    email = os.environ['INPUT_COMMITTER_EMAIL']

    # Group: Checkout git repository
    subprocess.run(['echo', '::group::Checkout git repository'])

    subprocess.run(['git', 'fetch', '--prune', '--unshallow', 'origin',  head_ref])
    subprocess.run(['git', 'checkout',  head_ref])

    subprocess.run(['echo', '::endgroup::'])

    # Group: Configure Git
    subprocess.run(['echo', '::group::Configure Git'])

    subprocess.run(['git', 'config', 'user.name', username])
    subprocess.run(['git', 'config', 'user.email',  email])

    subprocess.run(['echo', '::endgroup::'])

    # Group: Generate Changelog
    subprocess.run(['echo', '::group::Generate Changelog'])
    # Initialize the Changelog CI
    ci = ChangelogCI(
        repository, event_path, filename=filename,
        config_file=config_file, token=token
    )
    # Run Changelog CI
    ci.run()

    subprocess.run(['echo', '::endgroup::'])
