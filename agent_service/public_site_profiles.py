"""Code-owned public GET capabilities shared by browser, recovery and presets."""
from urllib.parse import urlencode, urlsplit


PROFILES = {
    'ikea_us': dict(start='https://www.ikea.com/us/en/', search='/us/en/search/',
        target=r'/us/en/p/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{8}/',
        region='#product-list', links='[data-testid="plp-product-card"][data-product-number] a.plp-price-module__product-link[href]',
        main='main', fixed={}, submit='button', button_name='',static=None,asset_query='none',support=[]),
    'python_docs': dict(start='https://docs.python.org/3/', search='/3/search.html',
        target=r'/3/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.html',
        region='#search-results', links='a[href]', main='div.body', fixed={}, submit='button', button_name='',
        static=r'/3/_static/[A-Za-z0-9_.-]+\.(?:js|css|svg|png|ico|woff2?)', asset_query='v',
        support=[('/3/searchindex.js','script'),('/3/_static/glossary.json','fetch')]),
    'python_org': dict(start='https://www.python.org/', search='/search/',
        target=r'/(?:about|doc|downloads|events|community|success-stories|psf)/[A-Za-z0-9_./-]+/',
        region='.list-recent-events', links='a[href]', main='#content', fixed={'submit':''}, submit='button', button_name='submit',
        static=r'/static/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:js|css|svg|png|ico|woff2?|ttf|eot)', asset_query='none',support=[]),
    'pandas_docs': dict(start='https://pandas.pydata.org/docs/search.html', search='/docs/search.html',
        target=r'/docs/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.html',
        region='#search-results', links='a[href]', main='article.bd-article', fixed={}, submit='enter', button_name='',
        static=r'/docs/_static/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:js|css|svg|png|ico|woff2?)', asset_query='pandas',
        support=[('/docs/searchindex.js','script')]),
    'django_docs': dict(start='https://docs.djangoproject.com/en/5.2/', search='/en/5.2/search/',
        target=r'/en/5\.2/(?:[A-Za-z0-9_.-]+/)+',
        region='dl.search-links', links='h2.result-title a[href]', main='article#docs-content',
        fixed={'category':''}, submit='button', button_name='',static=None,asset_query='none',support=[]),
}

PUBLIC_SITES = {
    'IKEA 美国（办公采购只读检索）': ('ikea_us', PROFILES['ikea_us']['start']),
    'Python 文档（公开检索）': ('python_docs', PROFILES['python_docs']['start']),
    'Python 官网（公开检索）': ('python_org', PROFILES['python_org']['start']),
    'pandas 文档（公开检索）': ('pandas_docs', PROFILES['pandas_docs']['start']),
    'Django 文档（公开检索）': ('django_docs', PROFILES['django_docs']['start']),
}


def public_search_url(config):
    profile = PROFILES[config['site']]
    return 'https://' + urlsplit(profile['start']).netloc + profile['search'] + '?' + urlencode(
        {'q': config['query'], **profile['fixed']})
