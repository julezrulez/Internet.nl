import os
import pytest
import subprocess
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from git import Repo
from git.exc import InvalidGitRepositoryError


DEFAULT_BROWSER_WIDTH = 1280
DEFAULT_BROWSER_HEIGHT = 1024


@pytest.fixture
def firefox_options(firefox_options):
    width = os.getenv('IT_BROWSER_WIDTH', DEFAULT_BROWSER_WIDTH)
    height = os.getenv('IT_BROWSER_HEIGHT', DEFAULT_BROWSER_HEIGHT)
    firefox_options.add_argument('--width={}'.format(width))
    firefox_options.add_argument('--height={}'.format(height))
    return firefox_options


def pytest_configure(config):
    pip_list_out, unused_err = subprocess.Popen(['pip','list'], stdout=subprocess.PIPE).communicate()

    try:
        # Assumes that the tests are being run from the tests/it subdirectory.
        r = Repo('/app')
        git_describe_tags_out = r.git.describe(tags=True)
        git_describe_branch_out = r.git.describe(all=True)
    except InvalidGitRepositoryError:
        git_describe_tags_out = 'Unknown'
        git_describe_branch_out = 'Unknown'

    config._metadata['Internet.NL Git Describe Tags'] = git_describe_tags_out
    config._metadata['Internet.NL Git Describe Branch'] = git_describe_branch_out
    config._metadata['Internet.NL Pip List'] = pip_list_out.decode('utf-8')
    config._metadata['Internet.NL Base Image'] = os.environ.get('INTERNETNL_BASE_IMAGE', 'Unknown')
