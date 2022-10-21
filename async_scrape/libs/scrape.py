from time import sleep
from datetime import datetime
from requests_html import HTMLSession
import logging
from requests import Response
from http.client import HTTPResponse

from .base_scrape import BaseScrape
from ..utils.header_vars import random_header_vars
from ..utils.errors import HttpResponseStatusError


class Scrape(BaseScrape):
    def __init__(self,
                 post_process_func: callable,
                 post_process_kwargs: dict = {},
                 fetch_error_handler: callable = None,
                 use_proxy: bool = False,
                 proxy: str = None,
                 pac_url: str = None,
                 consecutive_error_limit: int = 100,
                 attempt_limit: int = 5,
                 rest_between_attempts: bool = True,
                 rest_wait: int = 60,
                 call_rate_limit: int = None,
                 randomise_headers: bool = False
                 ):
        """Class for scrapping webpages

        args:
        ----
        post_process_func: callable
            for processing html
        post_process_kwargs: dict = {}
            kwargs for use in post processing
        fetch_error_handler: callable = None
            the function to be called if an
            error is experienced during _fetch. Passes in:
            url, error as arguments
        use_proxy: bool = False
            should a proxy be used
        proxy: str = None
            what is the address of the proxy ONLY VALID IF
            PROXY IS TRUE
        pac_url: str = None
            the location of the pac information ONLY VALID IF
            PROXY IS TRUE
        consecutive_error_limit: int = 100
            the number of times an error can be experienced 
            in a row before the scrape is cancelled and a new round is started
        attempt_limit: int = 5
            number of times a url can be attempted before it's abandoned
        rest_between_attempts: bool = True
            should the program rest between scrapes
        rest_wait: int = 60
            how long should the program rest for ONLY VALID IF
            REST_BETWEEN_SCRAPES IS TRUE
        call_rate_limit: int = None
            Should the rate of calls be limited. Fingure is calls per minute.
        randomise_headers: bool = False
            should the headers be randomised after each request
        """
        # Init super
        super().__init__(
            use_proxy=use_proxy,
            proxy=proxy,
            pac_url=pac_url,
            call_rate_limit=call_rate_limit
        )
        self.post_process = post_process_func
        self.post_process_kwargs = post_process_kwargs
        self.randomise_headers = randomise_headers
        self.headers = random_header_vars(self.header_vars)
        self.fetch_error_handler = fetch_error_handler
        self.session = HTMLSession()
        # Define allowed errors
        self.acceptable_errors = (HttpResponseStatusError,)
        self.consecutive_error_limit = consecutive_error_limit
        self.consecutive_error_count = 0
        # Define criteria for looping multiple attempts
        self.attempt_limit = attempt_limit
        self.rest_between_attempts = rest_between_attempts
        self.rest_wait = rest_wait
        self.tracker = None
        self.cur_err = None

    def _proxy(self):
        # Start the pac session
        self._get_pac_session()
        self.session = self.pac_session

    def _request(self, url: str):
        return self.session.get(url, headers=self.headers)

    def _fetch(self, url: str):
        """Function to fetch HTML from url

        args:
        ----
        url:str
            url to be requested

        returns:
        ----
        list
        """
        resp = None
        status = None
        # Make the request
        try:
            if url:
                resp = self._request(url)
                if isinstance(resp, Response):
                    status = resp.status_code
                    html = resp.text
                elif isinstance(resp, HTTPResponse):
                    status = resp.status
                    html = resp.read()
                else:
                    raise TypeError(
                        "resp should be of type HTTPResponse or Response")
                if status != 200:
                    raise HttpResponseStatusError(
                        f"url responded with a http status of {status}")
                func_resp = self.post_process(
                    html=html, resp=resp, **self.post_process_kwargs) \
                    if html is not None else None
                # Reset self.acceptable_error_count if all goes fine
                self.consecutive_error_count = 0
                return {"url": url, "func_resp": func_resp, "status": status, "error": None}
        except Exception as e:
            # Set the current error - increment if the same error
            if type(e) == self.cur_err:
                self.consecutive_error_count += 1
            else:
                self.cur_err = type(e)
                self.consecutive_error_count = 1
            # Check if acceptabe error limit has been reached
            # this prevents functions from carrying on after a site has started blocking calls
                logging.warning(
                    f"Consecutive error limit reached - {e} - consecutive count at {self.consecutive_error_count}/{self.consecutive_error_limit}")
            # Check for error handler
            if self.fetch_error_handler:
                logging.info(
                    f"Error passed to {self.fetch_error_handler.__name__}")
                # Run the error handler
                self.fetch_error_handler(url, e)
            # Check if acceptable error
            if type(e) in self.acceptable_errors:
                logging.error(
                    f"Acceptable error in request or post processing {url} - {e}")
            # Raise error
            else:
                logging.error(
                    f"Unhandled error in request or post processing {url} - {e}")
                if f"{e}" == "":
                    raise e
            return {"url": url, "func_resp": None, "status": status, "error": e}

    # run from terminal
    def scrape_all(self, urls: list):
        """"Function scraping html from urls and passing 
        them through the post processing function

        args:
        ----
        urls - list - the pages to be scraped

        returns:
        ----
        list of dicts
            EG [{
                "url":"http://google.com",
                "success":True,
                "status":200
            }]
        """
        self.start_job()
        if self.use_proxy:
            self._proxy()
        # Establish urls
        if not len(urls):
            return []
        # Set a dataframe for tracking the url attempts
        self.tracker = {
            u: {"scraped": False, "attempts": 0}
            for u in urls
        }
        resps = dict()
        scrape_urls = set(urls)
        all_failed_urls = set()
        logging.info(f"{len(urls)} unique urls from {len(scrape_urls)}")
        while len(scrape_urls):
            init_len = len(scrape_urls)
            self.reset_pages_scraped()
            self.total_to_scrape = len(scrape_urls)
            # Run the scrapes
            scrape_resps = []
            st_time = datetime.now()
            for i, url in enumerate(scrape_urls):
                scrape_resps.append(self._fetch(url))
                self.increment_pages_scraped()
                # Regenerate headers
                if self.randomise_headers:
                    self.headers = random_header_vars(self.header_vars)
                # Rest if rate limiting
                t = self.rate_limit_time(i, st_time)
                self.rate_limit_pause(t)
            # Process responses
            scrape_urls, new_resps, failed_urls = \
                self.handle_responses(scrape_urls, scrape_resps, init_len)
            resps |= new_resps
            all_failed_urls |= failed_urls
            # Sleep before running again
            # - shutdown must have been initiated
            # - there must still be urls to scrape
            # - the rest between attempt flag must be set to True
            if len(scrape_urls) \
                    and self.rest_between_attempts:
                logging.info(f"Sleeping for {self.rest_wait} seconds")
                sleep(self.rest_wait)
        logging.info(
            f"Scraping complete {len(all_failed_urls)}/{len(set(urls))} urls failed")
        # Convert resps back
        resps = [v for _, v in resps.items()]
        # end the job
        self.end_job()
        return resps

    # run from terminal
    def scrape_one(self, url: str):
        """"Function scraping html from a single url and passing 
        it through the post processing function

        args:
        ----
        url - str - the pages to be scraped

        returns:
        ----
        list of dicts
            EG [{
                "url":"http://google.com",
                "success":True,
                "status":200
            }]
        """
        if self.use_proxy:
            self._proxy()
        for _ in range(self.attempt_limit):
            resp = self._fetch(url)
            if resp["status"]:
                break
            elif self.rest_between_attempts:
                logging.info(f"Sleeping for {self.rest_wait} seconds")
                sleep(self.rest_wait)
        return resp