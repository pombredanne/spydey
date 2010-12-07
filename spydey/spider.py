"""
A simple web spider with several recursion strategies.

Many command options taken from wget;
some ideas from http://ericholscher.com/projects/django-test-utils/
"""
import collections
import httplib2
import logging
import lxml.html
import patternize
import posixpath
import pprint
import random
import re
import sys
import time
import urlparse

logger = logging.getLogger('spydey')
try:
    import fabulous.color
    fab = True
except ImportError:
    fab = False

if fab and sys.stderr.isatty():
    colorizer = fabulous.color
else:
    class _noop_colorizer:
        def __getattr__(self, *name):
            return lambda s: s
    colorizer = _noop_colorizer()

PROFILE_REPORT_SIZE=20

queuetypes = {}

class FifoUrlQueue(object):
    """
    A URL queue is responsible for storing a queue of unique URLs,
    removing duplicates, and deciding (via the pop() method) which
    URL to visit next.

    This base class pops URLs in FIFO order, so it does a
    breadth-first traversal of the site.
    """
    # This could subclass list, but I want to limit the API to a
    # subset of list's API.

    def __init__(self):
        self.urls = collections.deque()
        self.known_urls = set()
        self.referrers = {}  # first referrer only.

    def __len__(self):
        return len(self.urls)

    def append(self, url, referrer=None):
        if url not in self.known_urls:
            self.urls.append(url)
            self.known_urls.add(url)
            self.referrers[url] = referrer

    def extend(self, urls, referrer=None):
        # Is there a more efficient way to do this
        # while still only inserting each url once?
        for url in urls:
            self.append(url, referrer=referrer)

    def pop(self):
        return self.urls.popleft()

queuetypes['breadth-first'] = FifoUrlQueue

class RandomizingUrlQueue(FifoUrlQueue):

    """A URL Queue that pops URLs off the queue in random order.

    This turns out to not feel very random in behavior, because often
    the URL space is dominated by a few similar patterns, so we have a
    high likelihood of spending a lot of time on similar leaf nodes.
    """
    def __init__(self):
        self.urls = []
        self.known_urls = set()
        self.referrers = {}

    def pop(self):
        i = random.randint(0, len(self.urls) -1)
        logger.warn('Randomly popping %d of %d' % (i, len(self.urls)))
        # This is O(N), a dict keyed by ints might be a better storage.
        return self.urls.pop(i)

queuetypes['random'] = RandomizingUrlQueue

class DepthFirstQueue(FifoUrlQueue):

    """
    Depth-first traversal. Since we don't have a site map to follow,
    we're not walking a tree, but rather a (probably cyclic) directed
    graph. So we use a LIFO queue in typical depth-first fashion, but
    also, to get far away from the root as fast as possible, new links
    are appended in order of the number of path elements in the URL.

    In practice this means we quickly walk to the end of a branch and
    then spend a lot of time on similar leaf nodes before exploring
    another branch.
    """

    def extend(self, urls, referrer=None):
        urls.sort(key=lambda s: s.count('/'))
        return FifoUrlQueue.extend(self, urls, referrer)

    def pop(self):
        return self.urls.pop()

queuetypes['depth-first'] = DepthFirstQueue

class HybridTraverseQueue(DepthFirstQueue):
    """
    Alternate between depth-first and breadth-first traversal
    behavior.
    """
    def __init__(self):
        super(HybridTraverseQueue, self).__init__()
        self.next = self.urls.pop

    def pop(self):
        if self.next == self.urls.pop:
            self.next = self.urls.popleft
            logger.debug('next: left')
        else:
            self.next = self.urls.pop
            logger.debug('next: right')
        popped = self.next()
        return popped

queuetypes['hybrid'] = HybridTraverseQueue

class PatternPrioritizingUrlQueue(RandomizingUrlQueue):
    """
    An attempt at discovering different sections of a website quickly.
    We classify links with a primitive pattern-recognition algorithm, and
    prefer links whose patterns we haven't seen before.

    Classification uses a heuristic: the first part of the path,
    followed by the rest of the path converted into regex patterns.

    Whenever there are no high-priority URLs -- those whose patterns
    we haven't seen yet -- we fall back to RandomizingUrlQueue
    behavior, and pick a random URL from the remaining low-priority
    URLs.
    """
    def __init__(self):
        super(PatternPrioritizingUrlQueue, self).__init__()
        self.priority_urls = collections.deque()
        self.known_patterns = {}
        self.referrers = {}

    def make_pattern(self, s):
        path = urlparse.urlparse(s).path.strip('/')
        if not path:
            return ''
        parts = posixpath.normpath(path).split('/')
        parts = parts[:1] + [patternize.patternize(p) for p in parts[1:]]
        return '/'.join(parts)

    def append(self, url, referrer=None):
        if url in self.known_urls:
            return
        self.known_urls.add(url)
        self.referrers[url] = referrer
        new_pattern = self.make_pattern(url)
        if new_pattern in self.known_patterns:
            # put it in the low priority pile.
            self.urls.append(url)
            self.known_patterns[new_pattern] += 1
        else:
            logger.debug(colorizer.red('NEW PATTERN!') + new_pattern)
            self.priority_urls.append(url)
            self.known_patterns[new_pattern] = 1

    def extend(self, urls, referrer=None):
        # We actually want to visit the shallowest new-patterned URLs first.
        urls = set(urls)
        urls = sorted(urls, key=lambda s: s.count('/'), reverse=True)
        for url in urls:
            self.append(url, referrer)

    def pop(self):
        logger.debug(colorizer.green('LENGTH: known URLs: %d; new pattern queue: %d; old pattern queue: %d' % (len(self.known_urls), len(self.priority_urls), len(self.urls))))
        if self.priority_urls:
            return self.priority_urls.pop()
        return RandomizingUrlQueue.pop(self)

    def __len__(self):
        return len(self.urls) + len(self.priority_urls)


queuetypes['pattern'] = PatternPrioritizingUrlQueue


class Spider(object):

    """A simple web spider that doesn't yet do much beyond offer
    pluggable traversal strategies, and report HTTP status for each
    visited URL.
    """

    def __init__(self, url, opts):
        self.opts = opts
        self.base_url = url
        self.domain = urlparse.urlparse(url).netloc
        self.queue = queuetypes[opts.traversal]()
        self.queue.append(url)
        self.http = httplib2.Http(timeout=opts.timeout or None)
        self.fetchcount = 0
        self.reject = [(s, re.compile(s)) for s in (self.opts.reject or [])]
        self.accept = [(s, re.compile(s)) for s in (self.opts.accept or [])]
        self.slowest_urls = []

    def sleep(self):
        """Maybe wait before doing next download.
        """
        if self.opts.wait is not None:
            time.sleep(self.opts.wait)
        elif self.opts.random_wait is not None:
            time.sleep(random.uniform(0, 2 * self.opts.random_wait))

    def fetch_one(self, url):
        """Fetch a single URL.
        """
        if self.opts.profile:
            start = time.time()
        (response, content) = self.http.request(url)
        if self.opts.profile:
            elapsed = time.time() - start
            self.slowest_urls.append((elapsed, url))
            self.slowest_urls.sort(reverse=True)
            self.slowest_urls = self.slowest_urls[:PROFILE_REPORT_SIZE]
        else:
            logger.debug('fetched %r' % url)
            elapsed = None
        self.fetchcount += 1
        return (response, content, elapsed)

    def handle_result(self, url, response, data, elapsed):
        # TODO: options to store downloads, report different things, etc.
        status = response['status']
        if int(status) < 300:
            status = colorizer.green(status)
            level = logging.INFO
        elif int(status) < 400:
            # Looks like httplib2 hides redirects from us,
            # as it automatically follows them. Hmm.
            status = colorizer.cyan(status)
            level = logging.INFO
        elif int(status) == 404:
            status = colorizer.magenta(status)
            level = logging.WARN
        else:
            status = colorizer.red(status)
            level = logging.ERROR
        status = colorizer.bold(status)
        msg = '%d. %s %s' % (self.fetchcount, status, colorizer.blue(url))
        if self.opts.profile:
            msg = '%s  (%0.3f secs)' % (msg, elapsed)
        if self.opts.log_referrer:
            msg = '%s  (from %s)' % (msg, self.queue.referrers.get(url, None))
        logger.log(level, msg)

    def crawl(self):
        while self.queue:
            url = self.queue.pop()
            try:
                response, data, elapsed = self.fetch_one(url)
            except AttributeError:
                # httplib bug: socket is None, means no connection.
                logger.error("Failure connecting to %s" % url)
                continue
            # Might have followed a redirect. Need to fix our idea of
            # URL since we use that to fix relative links...
            if response.has_key('content-location'):
                logger.debug('redirected from %r to %r' % (url, response['content-location']))
                url = response['content-location']
            self.handle_result(url, response, data, elapsed)
            if self.opts.max_requests and self.fetchcount >= self.opts.max_requests:
                logger.info("Stopping after %d requests." % self.fetchcount)
                break
            if self.opts.recursive:
                urls = self.get_urls(url, response, data)
                self.queue.extend(urls, referrer=url)
                # fabulous doesn't deal well w/ this:
                #logger.debug("Adding new URLs from %r:\n%s" % (
                #        url, pprint.pformat(urls, indent=2)))
                self.sleep()
        if isinstance(self.queue, PatternPrioritizingUrlQueue):
            print "Pattern count summary:"
            patterns = [(v, k) for (k, v) in self.queue.known_patterns.items()]
            patterns = sorted(patterns)
            pprint.pprint([(k, v) for (v, k) in patterns])
        if self.opts.profile:
            print "Slowest %d URLs:" % PROFILE_REPORT_SIZE
            pprint.pprint(self.slowest_urls)

    def filter_links(self, links):
        # Assumes links are absolute, and are tuples as returned by iterlinks().
        for (el, attr, link, pos) in links:
            # Discard fragment name, eg http://foo/#bar -> http://foo/
            (scheme, netloc, path, query, frament) = urlparse.urlsplit(
                link, allow_fragments=False)
            fragment = ''
            # For some reason, sometimes the fragment ends up in the path.
            path = path.split('#', 1)[0]
            link = urlparse.urlunsplit((scheme, netloc, path, query, fragment))

            # We could stand to do some other normalization here, eg.
            # strip trailing slashes from the path - but that breaks
            # referrer logging on a site that redirects 'foo' to 'foo/'.

            # Check base URL if we're not spanning across hosts.
            if not self.opts.span_hosts:
                if netloc != self.domain:
                    logger.debug("Skipping %r from foreign domain" % link)
                    continue

            # Patterns to explicitly accept or reject.
            if self.accept:
                skip = True
            else:
                skip = False
            for pattern, regex in self.accept:
                if regex.search(link):
                    logger.debug("Allowing %r, matches accept pattern %r" % (link, pattern))
                    skip = False
                    break
            for pattern, regex in self.reject:
                if regex.search(link):
                    logger.debug("Skipping %r, matches reject pattern %r" % (link, pattern))
                    skip = True
                    break
            if skip:
                continue

            if el.tag == 'a':
                if self.opts.no_parent:
                    # Only applies to pages, not js, stylesheets or
                    # other resources.
                    if not link.startswith(self.base_url):
                        logger.debug("Skipping parent or sibling %r" % link)
                        continue
                yield link

            elif el.tag == 'form' and attr == 'action':
                # Unless we can guess how to fill out the form,
                # following these would make no sense at all.
                continue

            elif self.opts.page_requisites:
                logger.debug("getting page req. %r from (%r, %r)" % (link, el, attr))
                yield link
            else:
                logger.debug("Skipping %r from (%r, %r)" % (link, el, attr))
                continue

    def get_urls(self, url, response, data):
        logger.debug("getting more urls from %s..." % url)
        if is_html(response):
            tree = lxml.html.document_fromstring(data)
            tree.make_links_absolute(url, resolve_base_href=True)
            links = self.filter_links(tree.iterlinks())
            return list(links)
        else:
            # TODO: parse resource links from CSS.
            return []

def is_html(response):
    return response.get('content-type', '').lower().startswith('text/html')

def main():
    """
    Many command-line options were deliberately copied from wget.
    """
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option("-r", "--recursive", action="store_true", default=False,
                      help="recur into subdirectories")
    parser.add_option('-p', '--page-requisites', action="store_true",
                      default=False,
                      help="get all images, etc. needed to display HTML page.")
    parser.add_option('--no-parent', action="store_true", default=False,
                      help="don't ascend to the parent directory.")
    parser.add_option('-R', '--reject', action="append",
                      help="Regex for filenames to reject. May be given multiple times.")
    parser.add_option('-A', '--accept', action="append",
                      help="Regex for filenames to accept. May be given multiple times.")

    parser.add_option('-t', '--traversal', action="store",
                      default="breadth-first",
                      choices=sorted(queuetypes.keys()),
                      help="Recursive traversal strategy. Choices are: %s"
                      % ', '.join(sorted(queuetypes.keys())))

    parser.add_option("-H", "--span-hosts", action="store_true", default=False,
                      help="go to foreign hosts when recursive.")
    parser.add_option("-w", "--wait", default=None, type=float,
                      help="wait SECONDS between retrievals.")
    parser.add_option("--random-wait", default=None, type=float,
                      help="wait from 0...2*WAIT secs between retrievals.")
    parser.add_option("--loglevel", default='INFO', help="Log level.")
    parser.add_option("--log-referrer", action="store_true", default=False,
                      help="Log referrer URL for each request.")
    parser.add_option("--transient-log", default=False, action="store_true",
                      help="Use Fabulous transient logging config.")

    parser.add_option("--max-requests", default=0, type=int,
                      help="Maximum number of requests to make before exiting.")

    parser.add_option("-T", "--timeout", default=30, type=int,
                      help="Set the network timeout in seconds. 0 means no timeout.")

    parser.add_option("-P", "--profile", default=False, action="store_true",
                      help="Print the time to download each resource, and a summary of the %d slowest at the end." % PROFILE_REPORT_SIZE)

    (options, args) = parser.parse_args()
    loglevel = getattr(logging, options.loglevel.upper(), 'INFO')
    url = args.pop(0)
    spider = Spider(url, options)
    if options.transient_log and fab:
        import fabulous.logs
        fabulous.logs.basicConfig(level=loglevel)
    else:
        logging.basicConfig(level=loglevel)
    return spider.crawl()

if __name__ == '__main__':
    import sys
    sys.exit(main())
