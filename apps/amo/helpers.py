import collections
import json as jsonlib
import random
import re
from operator import attrgetter

from django.conf import settings
from django.forms import CheckboxInput
from django.utils import translation
from django.utils.encoding import smart_unicode
from django.template import defaultfilters

from babel import Locale
from babel.support import Format
import caching.base as caching
import jinja2
from jingo import register, env
from tower import ugettext as _, strip_whitespace

import amo
from amo import utils, urlresolvers
from translations.query import order_by_translation
from translations.helpers import truncate

# Yanking filters from Django.
register.filter(defaultfilters.slugify)

# Registering some utils as filters:
urlparams = register.filter(utils.urlparams)
register.filter(utils.epoch)
register.filter(utils.isotime)
register.function(dict)
register.function(utils.randslice)


@register.filter
def link(item):
    html = """<a href="%s">%s</a>""" % (item.get_url_path(),
                                        jinja2.escape(item.name))
    return jinja2.Markup(html)


@register.filter
def xssafe(value):
    """
    Like |safe but for strings with interpolation.

    By using |xssafe you assert that you have written tests proving an
    XSS can't happen here.
    """
    return jinja2.Markup(value)


@register.filter
def babel_datetime(t, format='medium'):
    return _get_format().datetime(t, format=format) if t else ''


@register.function
def locale_url(url):
    """Take a URL and give it the locale prefix."""
    prefixer = urlresolvers.get_url_prefix()
    script = prefixer.request.META['SCRIPT_NAME']
    parts = [script, prefixer.locale, url.lstrip('/')]
    return '/'.join(parts)


@register.inclusion_tag('includes/refinements.html')
@jinja2.contextfunction
def refinements(context, items, title, thing):
    d = dict(context.items())
    d.update(items=items, title=title, thing=thing)
    return d


@register.function
def url(viewname, *args, **kwargs):
    """Helper for Django's ``reverse`` in templates."""
    add_prefix = kwargs.pop('add_prefix', True)
    host = kwargs.pop('host', '')
    src = kwargs.pop('src', '')
    url = '%s%s' % (host, urlresolvers.reverse(viewname,
                                               args=args,
                                               kwargs=kwargs,
                                               add_prefix=add_prefix))
    if src:
        url = urlparams(url, src=src)
    return url


@register.function
def shared_url(viewname, addon, *args, **kwargs):
    """
    Helper specifically for addons or apps to get urls. Requires
    the viewname, addon (or app). It's assumed that we'll pass the
    slug into the args and we'll look up the right slug (addon or app)
    for you.

    Viewname should be a normal view eg: `addons.details` or `apps.details`.
    `addons.details` becomes `apps.details`, if we've passed an app, etc.

    A viewname such as `details` becomes `addons.details` or `apps.details`,
    depending on the add-on type.
    """
    slug = addon.app_slug if addon.is_webapp() else addon.slug
    prefix = 'apps' if addon.is_webapp() else 'addons'

    namespace, dot, latter = viewname.partition('.')

    # If `viewname` is prefixed with `addons.` but we're linking to a
    # webapp, the `viewname` magically gets prefixed with `apps.`.
    if namespace in ('addons', 'apps'):
        viewname = latter

    # Otherwise, we just slap the appropriate prefix in front of `viewname`.
    viewname = '.'.join([prefix, viewname])
    return url(viewname, *([slug] + list(args)), **kwargs)


@register.function
def services_url(viewname, *args, **kwargs):
    """Helper for ``url`` with host=SERVICES_URL."""
    kwargs.update({'host': settings.SERVICES_URL})
    return url(viewname, *args, **kwargs)


@register.filter
def paginator(pager):
    return Paginator(pager).render()


@register.filter
def impala_paginator(pager):
    t = env.get_template('amo/impala/paginator.html')
    return jinja2.Markup(t.render(pager=pager))


@register.filter
def mobile_paginator(pager):
    t = env.get_template('amo/mobile/paginator.html')
    return jinja2.Markup(t.render(pager=pager))


@register.function
def is_mobile(app):
    return app == amo.MOBILE


@register.function
def sidebar(app):
    """Populates the sidebar with (categories, types)."""
    from addons.models import Category
    if app is None:
        return [], []

    # We muck with query to make order_by and extra_order_by play nice.
    q = Category.objects.filter(application=app.id, weight__gte=0,
                                type=amo.ADDON_EXTENSION)
    categories = order_by_translation(q, 'name')
    categories.query.extra_order_by.insert(0, 'weight')

    Type = collections.namedtuple('Type', 'id name url')
    base = urlresolvers.reverse('home')
    types = [Type(99, _('Collections'), base + 'collections/')]

    shown_types = {
        amo.ADDON_PERSONA: urlresolvers.reverse('browse.personas'),
        amo.ADDON_DICT: urlresolvers.reverse('browse.language-tools'),
        amo.ADDON_SEARCH: urlresolvers.reverse('browse.search-tools'),
        amo.ADDON_THEME: urlresolvers.reverse('browse.themes'),
    }
    titles = dict(amo.ADDON_TYPES,
                  **{amo.ADDON_DICT: _('Dictionaries & Language Packs')})
    for type_, url in shown_types.items():
        if type_ in app.types:
            types.append(Type(type_, titles[type_], url))

    return categories, sorted(types, key=lambda x: x.name)


class Paginator(object):

    def __init__(self, pager):
        self.pager = pager

        self.max = 10
        self.span = (self.max - 1) / 2

        self.page = pager.number
        self.num_pages = pager.paginator.num_pages
        self.count = pager.paginator.count

        pager.page_range = self.range()
        pager.dotted_upper = self.num_pages not in pager.page_range
        pager.dotted_lower = 1 not in pager.page_range

    def range(self):
        """Return a list of page numbers to show in the paginator."""
        page, total, span = self.page, self.num_pages, self.span
        if total < self.max:
            lower, upper = 0, total
        elif page < span + 1:
            lower, upper = 0, span * 2
        elif page > total - span:
            lower, upper = total - span * 2, total
        else:
            lower, upper = page - span, page + span - 1
        return range(max(lower + 1, 1), min(total, upper) + 1)

    def render(self):
        c = {'pager': self.pager, 'num_pages': self.num_pages,
             'count': self.count}
        t = env.get_template('amo/paginator.html').render(**c)
        return jinja2.Markup(t)


def _get_format():
    lang = translation.get_language()
    locale = Locale(translation.to_locale(lang))
    return Format(locale)


@register.filter
def numberfmt(num, format=None):
    return _get_format().decimal(num, format)


@register.filter
def currencyfmt(num, currency):
    if num is None:
        return ''
    return _get_format().currency(num, currency)


def page_name(app=None):
    """Determine the correct page name for the given app (or no app)."""
    if app:
        return _(u'Add-ons for {0}').format(app.pretty)
    else:
        return _('Add-ons')


@register.function
@jinja2.contextfunction
def login_link(context):
    next = context['request'].path

    qs = context['request'].GET.urlencode()

    if qs:
        next += '?' + qs

    l = urlparams(urlresolvers.reverse('users.login'), to=next)
    return l


@register.function
@jinja2.contextfunction
def page_title(context, title, force_webapps=False):
    title = smart_unicode(title)
    if settings.APP_PREVIEW:
        base_title = loc('Apps Developer Preview')
    elif context.get('WEBAPPS') or force_webapps:
        base_title = loc('Apps Marketplace')
    else:
        base_title = page_name(context['request'].APP)
    return u'%s :: %s' % (title, base_title)


@register.function
@jinja2.contextfunction
def breadcrumbs(context, items=list(), add_default=True, crumb_size=40):
    """
    show a list of breadcrumbs. If url is None, it won't be a link.
    Accepts: [(url, label)]
    """
    if add_default:
        app = context['request'].APP
        crumbs = [(urlresolvers.reverse('home'), page_name(app))]
    else:
        crumbs = []

    # add user-defined breadcrumbs
    if items:
        try:
            crumbs += items
        except TypeError:
            crumbs.append(items)

    crumbs = [(url, truncate(label, crumb_size)) for (url, label) in crumbs]
    c = {'breadcrumbs': crumbs}
    t = env.get_template('amo/breadcrumbs.html').render(**c)
    return jinja2.Markup(t)


@register.function
@jinja2.contextfunction
def impala_breadcrumbs(context, items=list(), add_default=True, crumb_size=40):
    """
    show a list of breadcrumbs. If url is None, it won't be a link.
    Accepts: [(url, label)]
    """
    home = 'apps.home' if context.get('WEBAPPS') else 'home'
    if add_default:
        if context.get('WEBAPPS'):
            base_title = _('Apps Marketplace')
        else:
            base_title = page_name(context['request'].APP)
        crumbs = [(urlresolvers.reverse(home), base_title)]
    else:
        crumbs = []

    # add user-defined breadcrumbs
    if items:
        try:
            crumbs += items
        except TypeError:
            crumbs.append(items)

    crumbs = [(url, truncate(label, crumb_size)) for (url, label) in crumbs]
    c = {'breadcrumbs': crumbs, 'has_home': add_default}
    t = env.get_template('amo/impala/breadcrumbs.html').render(**c)
    return jinja2.Markup(t)


@register.filter
def json(s):
    return jsonlib.dumps(s)


@register.filter
def absolutify(url):
    """Takes a URL and prepends the SITE_URL"""
    if url.startswith('http'):
        return url
    else:
        return settings.SITE_URL + url


@register.filter
def strip_controls(s):
    """
    Strips control characters from a string.
    """
    # Translation table of control characters.
    control_trans = dict((n, None) for n in xrange(32) if n not in [10, 13])
    rv = unicode(s).translate(control_trans)
    return jinja2.Markup(rv) if isinstance(s, jinja2.Markup) else rv


@register.filter
def strip_html(s, just_kidding=False):
    """Strips HTML.  Confirm lets us opt out easily."""
    if just_kidding:
        return s

    if not s:
        return ''
    else:
        s = re.sub(r'&lt;.*?&gt;', '', smart_unicode(s, errors='ignore'))
        return re.sub(r'<.*?>', '', s)


@register.filter
def external_url(url):
    """Bounce a URL off outgoing.mozilla.org."""
    return urlresolvers.get_outgoing_url(unicode(url))


@register.filter
def shuffle(sequence):
    """Shuffle a sequence."""
    random.shuffle(sequence)
    return sequence


@register.function
def license_link(license):
    """Link to a code license, incl. icon where applicable."""
    if not license:
        return ''
    if not license.builtin:
        return _('Custom License')

    t = env.get_template('amo/license_link.html').render({'license': license})
    return jinja2.Markup(t)


@register.function
def field(field, label=None, **attrs):
    if label is not None:
        field.label = label
    # HTML from Django is already escaped.
    return jinja2.Markup(u'%s<p>%s%s</p>' %
                         (field.errors, field.label_tag(),
                          field.as_widget(attrs=attrs)))


@register.inclusion_tag('amo/category-arrow.html')
@jinja2.contextfunction
def category_arrow(context, key, prefix):
    d = dict(context.items())
    d.update(key=key, prefix=prefix)
    return d


@register.filter
def timesince(time):
    ago = defaultfilters.timesince(time)
    # L10n: relative time in the past, like '4 days ago'
    return _(u'{0} ago').format(ago)


@register.inclusion_tag('amo/recaptcha.html')
@jinja2.contextfunction
def recaptcha(context, form):
    d = dict(context.items())
    d.update(form=form)
    return d


@register.filter
def is_choice_field(value):
    try:
        return isinstance(value.field.widget, CheckboxInput)
    except AttributeError:
        pass


@register.inclusion_tag('amo/mobile/sort_by.html')
def mobile_sort_by(base_url, options=None, selected=None, extra_sort_opts=None,
                   search_filter=None):
    if search_filter:
        selected = search_filter.field
        options = search_filter.opts
        if hasattr(search_filter, 'extras'):
            options += search_filter.extras
    if extra_sort_opts:
        options_dict = dict(options + extra_sort_opts)
    else:
        options_dict = dict(options)
    if selected in options_dict:
        current = options_dict[selected]
    else:
        selected, current = options[0]  # Default to the first option.
    return locals()


@register.function
@jinja2.contextfunction
def media(context, url, key='MEDIA_URL'):
    """Get a MEDIA_URL link with a cache buster querystring."""
    if url.endswith('.js'):
        build = context['BUILD_ID_JS']
    elif url.endswith('.css'):
        build = context['BUILD_ID_CSS']
    else:
        build = context['BUILD_ID_IMG']
    return context[key] + utils.urlparams(url, b=build)


@register.function
@jinja2.contextfunction
def static(context, url):
    """Get a STATIC_URL link with a cache buster querystring."""
    return media(context, url, 'STATIC_URL')


@register.function
@jinja2.evalcontextfunction
def attrs(ctx, *args, **kw):
    return jinja2.filters.do_xmlattr(ctx, dict(*args, **kw))


@register.function
@jinja2.contextfunction
def side_nav(context, addon_type, category=None):
    app = context['request'].APP.id
    cat = str(category.id) if category else 'all'
    return caching.cached(lambda: _side_nav(context, addon_type, category),
                          'side-nav-%s-%s-%s' % (app, addon_type, cat))


def _side_nav(context, addon_type, cat):
    # Prevent helpers generating circular imports.
    from addons.models import Category, AddonType
    request = context['request']
    qs = Category.objects.filter(weight__gte=0)
    if addon_type != amo.ADDON_WEBAPP:
        qs = qs.filter(application=request.APP.id)
    sort_key = attrgetter('weight', 'name')
    categories = sorted(qs.filter(type=addon_type), key=sort_key)
    if cat:
        base_url = cat.get_url_path()
    else:
        base_url = AddonType(addon_type).get_url_path()
    ctx = dict(request=request, base_url=base_url, categories=categories,
               addon_type=addon_type, amo=amo)
    return jinja2.Markup(env.get_template('amo/side_nav.html').render(ctx))


@register.function
@jinja2.contextfunction
def site_nav(context):
    app = context['request'].APP.id
    return caching.cached(lambda: _site_nav(context), 'site-nav-%s' % app)


def _site_nav(context):
    # Prevent helpers from generating circular imports.
    from addons.models import Category
    request = context['request']
    types = amo.ADDON_EXTENSION, amo.ADDON_PERSONA, amo.ADDON_THEME
    qs = Category.objects.filter(application=request.APP.id, weight__gte=0,
                                 type__in=types)
    groups = utils.sorted_groupby(qs, key=attrgetter('type'))
    cats = dict((key, sorted(cs, key=attrgetter('weight', 'name')))
                for key, cs in groups)
    ctx = dict(request=request, amo=amo,
               extensions=cats.get(amo.ADDON_EXTENSION, []),
               personas=cats.get(amo.ADDON_PERSONA, []),
               themes=cats.get(amo.ADDON_THEME, []))
    return jinja2.Markup(env.get_template('amo/site_nav.html').render(ctx))


@register.filter
def premium_text(type):
    return amo.ADDON_PREMIUM_TYPES[type]


@register.function
def loc(s):
    """A noop function for strings that are not ready to be localized."""
    return strip_whitespace(s)


@register.function
def site_event_type(type):
    return amo.SITE_EVENT_CHOICES[type]
