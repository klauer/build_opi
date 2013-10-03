#!/usr/bin/env python
"""
build_opi.py

Simple BOY OPI screen generator from template and a list of PVs.
================================================================

PVs can be grouped together by regexes on the command line, such that
a setpoint/readback pair is generated and made into a read-write entry
in the interface.

Expects template.opi to have groups of widgets that match
(group)_[rw/ro/wo]_group'. (see `group_to_template`)
These widgets should have sub-widgets that use pvs %(readback_pv)s
and %(setpoint_pv)s, which will be substituted when generating the
screen.

Example:
    $ python build_opi.py --title 'ANC300 $(DEV) Axis $(AX)' \\
            --ignore 'AX[2-3]' --macros 'DEV=ANC300:,AX=1' \\
            --group '(.*)_IN' '\1_OUT' \\
            --substitute '(AX1:)' 'AX$(AX):' \\
            --sort type \\
            anc300_pvlist.txt anc300.opi

    anc300_pvlist has a list of PVs:
        ANC300:AX1:PV_IN
        ANC300:AX1:PV_OUT

    These PVs are changed via --substitute and --macros to:
        $(DEV)AX$(AX):PV_IN
        $(DEV)AX$(AX):PV_OUT
    and a group is made between (PV_IN, PV_OUT) by matching --group.
"""

from __future__ import print_function
import re
import sys
import copy
import argparse

import xml.etree.ElementTree as ET

import epics

templates = {}
rtype_groups = {'ai' : ('text', 'r'),
                'ao' : ('text', 'w'),

                'bi': ('binary', 'r'),
                'bo': ('binary', 'w'),

                'stringin': ('text', 'r'),
                'stringout': ('text', 'w'),

                'longin': ('text', 'r'),
                'longout': ('text', 'w'),

                'mbbi': ('menu', 'r'),
                'mbbo': ('menu', 'w'),

                'calc': ('text', 'r'),
                'calcout': ('text', 'r'),
                }

group_to_template = '%(group)s_%(access)s_group'

def read_template(fn):
    global templates

    tree = ET.parse(fn)
    root = tree.getroot()

    templates = dict((widget.find('name').text, {'widget': widget})
                     for widget in root.findall('widget'))

    for name, info in templates.items():
        widget = info['widget']
        height = int(widget.find('height').text)

        info['height'] = height
        root.remove(widget)
    return tree, root

def find_all_subwidgets(widget):
    for sub_widget in widget.findall('widget'):
        yield sub_widget
        for sub_sub in find_all_subwidgets(sub_widget):
            yield sub_sub

def scale_attributes(widget, attr, scale=1.0, type_=int):
    widgets = [widget] + list(find_all_subwidgets(widget))

    for w in widgets:
        w_attr = w.find(attr)
        value = type_(w_attr.text)
        w_attr.text = str(int(value * scale))

def add_widget(x, y, parent, widget_name, x_scale=1.0, y_scale=1.0,
               spacing=5.0,
               **info):
    try:
        widget = templates[widget_name]['widget']
    except KeyError:
        print('Template %s unavailable' % widget_name)
        return y

    widget = templates[widget_name]['widget']
    widget = copy.deepcopy(widget)

    sub_widgets = list(find_all_subwidgets(widget))
    if x_scale != 1.0:
        scale_attributes(widget, 'x', x_scale, type_=int)
        scale_attributes(widget, 'width', x_scale, type_=int)

    if y_scale != 1.0:
        scale_attributes(widget, 'y', y_scale, type_=int)
        scale_attributes(widget, 'height', y_scale, type_=int)

    x = x * x_scale
    y = y * y_scale

    widget.find('x').text = str(int(x * x_scale))
    widget.find('y').text = str(int(y * y_scale))

    # Take the parsed xml, convert it to string
    group_text = ET.tostring(widget)

    # Format it with all of the info
    group_text = group_text % info

    # Convert back to xml and insert into the parent node
    new_node = ET.fromstring(group_text)
    parent.append(new_node)

    return y + spacing + int(widget.find('height').text)

def make_display(root, pvs, x=0, y=0, title='', macros={},
                 sort='', **info):
    if title:
        y = add_widget(x, y, root, 'title_group', title=title, **info)

    if macros:
        m = root.find('macros')
        for mname, mvalue in macros.items():
            print('Macro set %s=%s' % (mname, mvalue))
            element = ET.SubElement(m, mname)
            element.text = mvalue

    sort_key = None
    if sort == 'pv':
        sort_key = 'desc_pv'
    elif sort == 'type':
        sort_key = 'template'
    elif sort in pvs[0]:
        sort_key = sort

    if sort_key is not None:
        pvs.sort(key=lambda item: item[sort_key])

    for pv_info in pvs:
        pv_info = copy.deepcopy(pv_info)
        pv_info.update(info)
        y = add_widget(x, y, root, pv_info['template'], **pv_info)

    return y

def sub_macros(text, macros):
    for from_, to in macros.items():
        text = text.replace(to, '$(%s)' % from_)
    return text

def expand_macros(text, macros):
    for from_, to in macros.items():
        text = re.sub('\$[({]%s[)}]' % from_, to, text)
    return text

def get_pv_info(macros, pvs=(), rtype=(), description=''):
    ret = {}
    if isinstance(pvs, str):
        pvs = (pvs, )

    if not rtype or len(rtype) != 2:
        if not macros:
            raise ValueError('Macros must be set to determine record types')

        rtype = [epics.caget('%s.RTYP' % expand_macros(pv, macros))
                 for pv in pvs]

    try:
        rtype_info = [rtype_groups[rt] for rt in rtype]
    except KeyError:
        print('Unknown record type: %s' % rt)
        return

    groups = [rt[0] for rt in rtype_info]
    access = [rt[1] for rt in rtype_info]
    if access == 2 * ['r'] or access == 2 * ['w']:
        raise ValueError('2 readbacks/setpoints in one group?')

    readback_pv = ''
    setpoint_pv = ''
    for pv, (group, access) in zip(pvs, rtype_info):
        if access == 'r':
            readback_pv = pv
            desc_pv = pv
        elif access == 'w':
            setpoint_pv = pv
            desc_pv = pv

    if readback_pv:
        desc_pv = readback_pv
        if setpoint_pv:
            access = 'rw'
        else:
            access = 'ro'
    elif setpoint_pv:
        desc_pv = setpoint_pv
        access = 'wo'
    else:
        raise ValueError('Need either readback/setpoint pv')

    if not description and macros:
        pv = expand_macros(desc_pv, macros)
        pv_desc = epics.caget('%s.DESC' % pv, timeout=0.1)
        if pv_desc is not None:
            description = pv_desc
    else:
        description = readback_pv

    template = group_to_template % locals()
    ret['readback_pv'] = readback_pv
    ret['setpoint_pv'] = setpoint_pv
    ret['desc'] = description
    ret['desc_pv'] = desc_pv
    ret['template'] = template
    print('Readback: %s setpoint: %s record type: %s description: %s template: %s' %
          (readback_pv, setpoint_pv, rtype, description, template))
    return ret

def display_from_pv_list(macros={}, others=[], groups=[], template='template.opi', **kwargs):
    tree, root = read_template(template)

    info = [get_pv_info(macros, pvs=pv) for pv in
                        others + groups]

    info = [pvinfo for pvinfo in info
            if pvinfo is not None]

    make_display(root, info, macros=macros, **kwargs)

    return tree

def main(pv_list='/epics/pv_lists/iocanc300.txt',
         output='output.opi',
         macros={}, group_pattern=None,
         ignore=[], title='title',
         substitute=[], **kwargs):

    def sub_pv(pv):
        if substitute:
            pat, sub = substitute
            return re.sub(pat, sub, pv)
        else:
            return pv

    pvs = [line.strip() for line in open(pv_list, 'rt').readlines()]
    pvs = [sub_pv(pv) for pv in pvs]
    pvs = [sub_macros(pv, macros) for pv in pvs]

    for pattern in ignore:
        for pv in list(pvs):
            m = re.search(pattern, pv)
            if m is not None:
                pvs.remove(pv)
                continue

    others = pvs
    groups = []
    pattern, replace = group_pattern
    for pv1 in list(others):
        if isinstance(pv1, tuple):
            continue

        pv2 = re.sub(pattern, replace, pv1)
        if pv1 != pv2 and pv2 in others:
            others.remove(pv1)
            others.remove(pv2)
            groups.append((pv1, pv2))

    print('unclassified ', '\n\t'.join(others))
    print('groups ', '\n\t'.join(str(group) for group in groups))
    tree = display_from_pv_list(macros=macros, title=title,
                                others=others, groups=groups,
                                **kwargs)
    tree.write('output.opi')

def parse_macro_string(m):
    macros = {}
    for entry in m.split(','):
        var, value = entry.split('=')
        macros[var] = value
    return macros

#(r'(.*)_IN$', r'\1_OUT', ),
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('pv_list_file')
    parser.add_argument('output_file')
    parser.add_argument('-i', '--ignore', nargs='*',
                        help='Ignore PVs matching these regular expression(s)')
    parser.add_argument('-t', '--title', default='title',
                        help='Title of the screen')
    parser.add_argument('-g', '--group', nargs=2,
                        help="""Group PVs together by regex:
                                e.g., --group (.*)_IN \1_OUT
                                would match PV_NAME_IN and PV_NAME_OUT
                                together and put them in the same group.""")
    parser.add_argument('-s', '--substitute', nargs=2,
                        help='Substitute text in PV names')
    parser.add_argument('-m', '--macros',
                        help='Macro string')
    parser.add_argument('-S', '--sort', choices=('desc', 'type', 'pv'),
                        help='Sort type')
    parser.add_argument('-x', '--scalex', type=float, default=1.0,
                        help='Scale x/width')
    parser.add_argument('-y', '--scaley', type=float, default=1.0,
                        help='Scale x/width')
    parser.add_argument('-T', '--template', default='template.opi',
                        help='Template filename')

    args = parser.parse_args()
    macros = parse_macro_string(args.macros)
    print('Input:', args.pv_list_file)
    print('Output:', args.output_file)
    print('Template:', args.template)
    main(args.pv_list_file, args.output_file,
         ignore=args.ignore,
         substitute=args.substitute, group_pattern=args.group,
         title=args.title, macros=macros, sort=args.sort,
         x_scale=args.scalex, y_scale=args.scaley, template=args.template)
