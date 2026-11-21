"""Extract actual patched methods for a small Qt ownership test, without RV engine stubs."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def function(source, signature):
    if source.count(signature) != 1:
        raise ValueError('Expected exactly one production function: ' + signature)
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    # These bounded methods have no brace-containing string/comment literals.
    while depth and end < len(source):
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    if depth:
        raise ValueError('Unbalanced production function: ' + signature)
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--legacy-negative', action='store_true')
    args = parser.parse_args()
    names = ('src/lib/app/RvCommon/RvApplication.cpp', 'src/lib/app/RvCommon/RvCommon/RvApplication.h',
             'src/lib/app/RvCommon/RvDocument.cpp')
    data = {name: (args.source / name).read_bytes() for name in names}
    cpp, header, document = (data[name].decode('utf-8') for name in names)
    field = re.search(r'^\s*(?:QPointer<RvPreferences>|RvPreferences\*)\s+m_prefDialog;', header, re.MULTILINE)
    if not field:
        raise ValueError('Missing actual cached preference member declaration')
    getter = function(cpp, 'RvPreferences* RvApplication::prefDialog()')
    methods = [getter]
    observer = ''
    if args.legacy_negative:
        if 'QPointer<' in field.group() or 'return m_prefDialog;' not in getter:
            raise ValueError('Negative control requires the actual old raw-cache source')
    else:
        if 'QPointer<RvPreferences>' not in field.group() or 'return m_prefDialog.data();' not in getter:
            raise ValueError('Expected guarded cache and out-of-line explicit raw-pointer return')
        close = function(cpp, 'void RvApplication::closeExistingAuxiliaryWindows()')
        if re.search(r'\b(?:console|networkWindow|prefDialog|profileManager)\s*\(', close):
            raise ValueError('Shutdown must not call a lazy dialog factory')
        destructor = function(document, 'RvDocument::~RvDocument()')
        if ('RvApp()->closeExistingAuxiliaryWindows();' not in destructor
                or 'RvApp()->existingNetworkWindow()' not in destructor
                or re.search(r'RvApp\(\)->(?:console|networkWindow|prefDialog|profileManager)\s*\(',
                             re.sub(r'//[^\n]*', '', destructor))):
            raise ValueError('Actual document destructor must use non-creating cleanup paths')
        observer = 'RvNetworkDialog* existingNetworkWindow() const { return m_networkDialog; }'
        if header.count(observer) != 1:
            raise ValueError('Missing non-creating network observer')
        methods.append(close)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'preference_member.inc').write_text(field.group().strip() + '\n', encoding='utf-8')
    (args.output / 'network_observer.inc').write_text(observer + '\n', encoding='utf-8')
    (args.output / 'production_methods.inc').write_text('\n\n'.join(methods) + '\n', encoding='utf-8')
    (args.output / 'source-proof.json').write_text(json.dumps({
        'source': str(args.source.resolve()), 'legacyNegativeOnly': args.legacy_negative,
        'sourceSha256': {name: hashlib.sha256(value).hexdigest() for name, value in data.items()},
        'scope': 'Actual production getter/member/shutdown bodies with real Qt ownership; RV engine context is minimal.'
    }, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
