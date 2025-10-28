import inspect
import os
import re
import types

import pymel.internal.apicache as apicache
import pymel.internal.parsers as parsers
import pymel.internal.startup
import pymel.util.arguments as arguments
import pymel.versions as versions

from pprint import pprint

from pymel.util.enum import Enum
from pymel.util.arguments import AddedKey, ChangedKey, RemovedKey

from past.builtins import basestring, unicode

THIS_FILE = inspect.getsourcefile(lambda: None)
THIS_DIR = os.path.dirname(THIS_FILE)

repodir = os.path.dirname(THIS_DIR)
cachedir = os.path.join(repodir, 'pymel', 'cache')

cacheversions = {
    'old': 2020,
    'new': 2021,
}

cachefiles = {key: 'mayaApi{}.py'.format(ver)
              for key, ver in cacheversions.items()}

DO_PREPROCESS = False

def preprocess(cache):
    apiClassInfo = cache[-1]
    # remove skipped entries
    for clsname in list(apiClassInfo):
        if parsers.ApiDocParser.shouldSkip(clsname):
            apiClassInfo.pop(clsname, None)
    for clsname, methName in parsers.XmlApiDocParser.SKIP_PARSING_METHODS:
        apiClassInfo.get(clsname, {})['methods'].pop(methName, None)

    for clsInfo in apiClassInfo.values():
        for overloads in clsInfo['methods'].values():
            for methInfo in overloads:
                argInfo = methInfo['argInfo']
                quals = methInfo.get('typeQualifiers', {})
                for argName, argQuals in list(quals.items()):
                    if argName not in argInfo or not argQuals:
                        del quals[argName]

    return cache

caches = {}
for key, cachefile in cachefiles.items():
    cachepath = os.path.join(cachedir, cachefile)
    cache_globals = {}
    cacheInst = apicache.ApiCache()
    data = cacheInst.read(path=cachepath)
    if DO_PREPROCESS:
        data = preprocess(data)
        cachepath_namebase, cachepath_ext = os.path.splitext(cachepath)
        preprocessed_path = cachepath_namebase + '.preprocessed' + cachepath_ext
        cacheInst.write(data, path=preprocessed_path)
    caches[key] = data

# we only care about the diffs of the classInfo
both, onlyOld, onlyNew, diffs = arguments.compareCascadingDicts(
    caches['old'][-1],
    caches['new'][-1],
    useAddedKeys=True, useChangedKeys=True)

################################################################################
# iteration utils

class AnyKey(object):
    '''Sentinel value to indicate all keys should be iterated over'''
    def __init__(self, comment):
        '''Init does nothing, just allows you to add a comment visible in the
        code'''
        pass


class NoValue(object):
    '''Sentinel value (distinct from None) to indicate no value'''
    pass


def iterDiffDictForKey(cascadingDict, multiKey, onlyDicts=False):
    '''Given a multiKey into a cascading dict, where each piece in the multiKey
    is either a fixed value, list of fixed values, or AnyKey, meaning to iterate
    over all keys at that level, yield the results gained from getting the last
    key in the multiKey'''
    if not multiKey:
        raise ValueError("multiKey must have at least one item")

    head, tail = multiKey[0], multiKey[1:]

    if head is AnyKey or isinstance(head, AnyKey):
        keyIter = cascadingDict.keys()
    elif isinstance(head, list):
        keyIter = head
    else:
        keyIter = [head]

    for key in keyIter:
        val = cascadingDict.get(key, NoValue)
        if val is NoValue:
            continue

        if not tail:
            # if there's no tail, we're done recursing...
            if not onlyDicts or isinstance(val, dict):
                yield (key,), val
        elif isinstance(val, dict):
            for subMultiKey, subItem in iterDiffDictForKey(val, tail,
                                                           onlyDicts=onlyDicts):
                yield (key,) + subMultiKey, subItem

# convience iterators at specific levels

def iterOverloadDiffs(onlyDicts=False):
    iterKey = (
        AnyKey('classname'),
        'methods',
        AnyKey('methodname'),
        AnyKey('overloadIndex'),
    )

    for item in iterDiffDictForKey(diffs, iterKey, onlyDicts=onlyDicts):
        yield item

#eliminate known diffs

################################################################################

# Doc for 'className' method got more verbose, and it became static
#'className': {0: {'doc': ChangedKey('Class name.', 'Returns the name of this class.')

iterKey = (
    AnyKey('classname'),
    'methods',
    'className',  # this is potentially confusing - the methodName IS 'className'
    AnyKey('overloadIndex'),
)

for _, overloadDiff in iterDiffDictForKey(diffs, iterKey):
    docDiff = overloadDiff.get('doc')
    if isinstance(docDiff, ChangedKey):
        if set([
                    docDiff.oldVal.lower().rstrip('.'),
                    docDiff.newVal.lower().rstrip('.'),
                ]) == set([
                    'class name',
                    'returns the name of this class',
                ]):
            del overloadDiff['doc']
    staticDiff = overloadDiff.get('static')
    if (isinstance(staticDiff, ChangedKey)
            and not staticDiff.oldVal
            and staticDiff.newVal):
        del overloadDiff['static']

################################################################################

# It's ok if it didn't have a doc, and now it does
def hasNewDoc(arg):
    if not isinstance(arg, dict):
        return False
    doc = arg.get('doc')
    if not doc:
        return False
    if isinstance(doc, AddedKey):
        return True
    if isinstance(doc, ChangedKey):
        if not doc.oldVal:
            return True
    return False

def removeDocDiff(arg):
    del arg['doc']
    return arg
arguments.deepPatch(diffs, hasNewDoc, removeDocDiff)

################################################################################

# It's ok if the doc is now longer
# (as long as it doesn't now include "\param" or "\return" codes)
def hasLongerDoc(arg):
    if not isinstance(arg, dict):
        return False
    doc = arg.get('doc')
    if not doc:
        return False
    if isinstance(doc, ChangedKey):
        if not doc.newVal.startswith(doc.oldVal):
            return False
        extraDoc = doc.newVal[len(doc.oldVal):]
        return '\\param' not in extraDoc and '\\return' not in extraDoc
    return False

arguments.deepPatch(diffs, hasLongerDoc, removeDocDiff)

################################################################################

# It's ok if the doc is now shorter, if it seems to have been truncated at a
# sentence end.
def wasTrimmedToSentence(arg):
    if not isinstance(arg, dict):
        return False
    doc = arg.get('doc')
    if not doc:
        return False
    if isinstance(doc, ChangedKey):
        if not doc.oldVal.startswith(doc.newVal):
            return False
        if not doc.newVal.endswith('.'):
            return False
        return doc.oldVal[len(doc.newVal)] == ' '
    return False

arguments.deepPatch(diffs, wasTrimmedToSentence, removeDocDiff)

################################################################################

# It's ok if the doc changed for a deprecated function

for multiKey, overloadDiff in iterOverloadDiffs(onlyDicts=True):
    overloadData = arguments.getCascadingDictItem(caches['new'][-1],
                                                  multiKey)
    if not overloadData.get('deprecated'):
        continue

    overloadDiff.pop('doc', None)

    # check for changed docs for params
    argInfoDiff = overloadDiff.get('argInfo')
    if isinstance(argInfoDiff, dict):
        for argDiffs in argInfoDiff.values():
            if not isinstance(argDiffs, dict):
                continue
            argDiffs.pop('doc', None)

################################################################################

# ignore changes in only capitalization or punctuation
# ...also strip out any "\\li " or <b>/</b> items
# ...or whitespace length...
ASCII_PUNCTUATION = """;-'"`,."""
UNICODE_PUNCTUATION = (unicode(ASCII_PUNCTUATION) \
                      # single left/right quote
                      + u'\u2018\u2019')
PUNCTUATION_TABLE = {ord(x): None for x in UNICODE_PUNCTUATION}
def strip_punctuation(input):
    return input.translate(PUNCTUATION_TABLE)


MULTI_SPACE_RE = re.compile('\s+')

def normalize_str(input):
    result = strip_punctuation(input.lower())
    result = result.replace(' \\li ', ' ')
    result = result.replace('<b>', '')
    result = result.replace('</b>', '')
    result = result.replace('\n', '')
    result = MULTI_SPACE_RE.sub(' ', result)
    return result

def same_after_normalize(input):
    if not isinstance(input, ChangedKey):
        return False
    if not isinstance(input.oldVal, basestring) or not isinstance(input.newVal, basestring):
        return False
    return normalize_str(input.oldVal) == normalize_str(input.newVal)

def returnNone(input):
    return None

arguments.deepPatch(diffs, same_after_normalize, returnNone)

################################################################################

# enums are now recorded in a way where there's no documentation for values...

# {'enums': {'ColorTable': {'valueDocs': {'activeColors': RemovedKey('Colors for active objects.'),
#                                         'backgroundColor': RemovedKey('Colors for background color.'),
#                                         'dormantColors': RemovedKey('Colors for dormant objects.'),
#                                         'kActiveColors': RemovedKey('Colors for active objects.'),
#                                         'kBackgroundColor': RemovedKey('Colors for background color.'),
#                                         'kDormantColors': RemovedKey('Colors for dormant objects.'),
#                                         'kTemplateColor': RemovedKey('Colors for templated objects.'),
#                                         'templateColor': RemovedKey('Colors for templated objects.')}},

iterKey = (
    AnyKey('classname'),
    'enums',
    AnyKey('enumname'),
)

for _, enumDiffs in iterDiffDictForKey(diffs, iterKey, onlyDicts=True):
    valueDocs = enumDiffs.get('valueDocs')
    if not isinstance(valueDocs, dict):
        continue
    if all(isinstance(val, arguments.RemovedKey) for val in valueDocs.values()):
        del enumDiffs['valueDocs']

################################################################################
# Enums that have new values added are ok
def enums_with_new_values(input):
    if not isinstance(input, ChangedKey):
        return False
    oldVal = input.oldVal
    newVal = input.newVal
    if not (isinstance(oldVal, Enum) and isinstance(newVal, Enum)):
        return False
    if oldVal.name != newVal.name:
        return False
    oldKeys = set(oldVal._keys)
    newKeys = set(newVal._keys)
    if not newKeys.issuperset(oldKeys):
        return False
    onlyNewKeys = newKeys - oldKeys
    prunedNewKeyDict = dict(newVal._keys)
    prunedNewDocDict = dict(newVal._docs)
    for k in onlyNewKeys:
        del prunedNewKeyDict[k]
        prunedNewDocDict.pop(k, None)
    if not prunedNewKeyDict == oldVal._keys:
        return False
    if not prunedNewDocDict == oldVal._docs:
        return False
    return True


arguments.deepPatch(diffs, enums_with_new_values, returnNone)
################################################################################
# new enums are ok

iterKey = (
    AnyKey('classname'),
    ['enums', 'pymelEnums'],
)

for _, enums in iterDiffDictForKey(diffs, iterKey, onlyDicts=True):
    for enumName, enumDiff in list(enums.items()):
        if isinstance(enumDiff, AddedKey):
            del enums[enumName]

################################################################################

# new methods are ok

iterKey = (
    AnyKey('classname'),
    'methods',
)

for multiKey, methods in iterDiffDictForKey(diffs, iterKey, onlyDicts=True):
    newMethods = []
    for methodName, methodDiff in list(methods.items()):
        if isinstance(methodDiff, AddedKey):
            del methods[methodName]
            newMethods.append(methodName)
        # may not be an entirely new method, but maybe there's new overloads?
        elif isinstance(methodDiff, dict):
            for key, overloadDiff in list(methodDiff.items()):
                if isinstance(overloadDiff, AddedKey):
                    del methodDiff[key]

    if not newMethods:
        continue

    clsname = multiKey[0]
    clsDiffs = diffs[clsname]

    # check if the new methods were invertibles, and clear up diffs due to that
    if len(newMethods) >= 2:
        invertibleDiffs = clsDiffs.get('invertibles')
        if not isinstance(invertibleDiffs, dict):
            continue
        # build up a set of all the invertibles in the new and old cache. 
        # Then, from the set of new invertibles, subtract out all new methods. 
        # If what's left over is the same as the oldInvertibles, we can ignore
        # the changes to the invertibles
        allInvertibles = {'old': set(), 'new': set()}
        for oldNew in ('old', 'new'):
            invertibles = caches[oldNew][-1][clsname]['invertibles']
            for setGet in invertibles:
                allInvertibles[oldNew].update(setGet)
        newInvertMinusNewMethods = allInvertibles['new'].difference(newMethods)
        if newInvertMinusNewMethods == allInvertibles['old']:
            del clsDiffs['invertibles']

    pymelMethodDiffs = clsDiffs.get('pymelMethods')
    if not isinstance(pymelMethodDiffs, dict):
        continue
    for newMethod in newMethods:
        if isinstance(pymelMethodDiffs.get(newMethod), AddedKey):
            del pymelMethodDiffs[newMethod]

################################################################################

# new args are ok

for multiKey, overloadDiff in iterOverloadDiffs(onlyDicts=True):
    # check to see if the ONLY change to args is AddedKeys..
    args = overloadDiff.get('args')
    if not isinstance(args, dict):
        continue

    if not all(isinstance(x, AddedKey) for x in args.values()):
        continue

    # Ok, args only had added keys - get a list of the names...
    newArgs = set(x.newVal[0] for x in args.values())

    # the args MUST also appear as AddedKeys in argInfo
    argInfo = overloadDiff.get('argInfo')
    if not isinstance(argInfo, dict):
        continue

    if not all(isinstance(argInfo.get(x), AddedKey) for x in newArgs):
        continue

    # ok, everything seems to check out - start deleting

    # we confirmed that all the diffs in args are AddedKey, remove them all!
    del overloadDiff['args']

    # remove newArgs from 'argInfo'
    for newArg in newArgs:
        del argInfo[newArg]

    # remove newArgs from defaults, types, typeQualifiers - these all key on
    # argName
    for subItemName in ('defaults', 'types', 'typeQualifiers'):
        subDict = overloadDiff.get(subItemName)
        if isinstance(subDict, AddedKey):
            subDict = subDict.newVal
            if set(subDict) == newArgs:
                del overloadDiff[subItemName]
        elif isinstance(subDict, dict):
            for newArg in newArgs:
                argDiff = subDict.get(newArg)
                if isinstance(argDiff, AddedKey):
                    del subDict[newArg]

    # remove newArgs from inArgs / outArgs - these are lists, and so key on
    # arbitrary indices
    for subItemName in ('inArgs', 'outArgs'):
        subDict = overloadDiff.get(subItemName)
        if isinstance(subDict, AddedKey):
            subList = subDict.newVal
            if set(subDict.newVal) == newArgs:
                del overloadDiff[subItemName]
        elif isinstance(subDict, dict):
            for key, val in list(subDict.items()):
                if isinstance(val, AddedKey) and val.newVal in newArgs:
                    del subDict[key]

################################################################################

# new classes are ok
for clsname, clsDiffs in list(diffs.items()):
    if isinstance(clsDiffs, AddedKey):
        del diffs[clsname]

################################################################################

# Lost docs

# these params or methods no longer have documentation in the xml... not great,
# but nothing the xml parser can do about that

# LOST_ALL_DETAIL_DOCS = {
#     ('MColor', 'methods', 'get', 1,),
#     ('MColor', 'methods', 'get', 2,),
# }
#
# for multiKey in LOST_ALL_DETAIL_DOCS:
#     try:
#         overloadInfo = arguments.getCascadingDictItem(diffs, multiKey)
#     except KeyError:
#         continue
#     if not isinstance(overloadInfo, dict):
#         continue
#
#     # deal with missing returnInfo doc
#     returnInfo = overloadInfo.get('returnInfo')
#     if isinstance(returnInfo, dict):
#         doc = returnInfo.get('doc')
#         if (isinstance(doc, arguments.RemovedKey)
#                 or (isinstance(doc, ChangedKey)
#                     and not doc.newVal)):
#             del returnInfo['doc']
#
#     # deal with missing param docs
#     argInfo = overloadInfo.get('argInfo')
#     if not isinstance(argInfo, dict):
#         continue
#     for argName, argDiff in argInfo.items():
#         if not isinstance(argDiff, dict):
#             continue
#         doc = argDiff.get('doc')
#         if (isinstance(doc, arguments.RemovedKey)
#                 or (isinstance(doc, ChangedKey)
#                     and not doc.newVal)):
#             del argDiff['doc']

# Temp - ignore all doc deletion diffs
for _, overloadDiff in iterOverloadDiffs(onlyDicts=True):
    # ignore method doc removal
    doc = overloadDiff.get('doc')
    if (isinstance(doc, arguments.RemovedKey)
            or (isinstance(doc, ChangedKey)
                and not doc.newVal)):
        del overloadDiff['doc']

    # ignore returnInfo doc removal
    returnInfo = overloadDiff.get('returnInfo')
    if isinstance(returnInfo, dict):
        doc = returnInfo.get('doc')
        if (isinstance(doc, arguments.RemovedKey)
                or (isinstance(doc, ChangedKey)
                    and not doc.newVal)):
            del returnInfo['doc']

    # ignore param doc removal
    for _, argDiff in iterDiffDictForKey(overloadDiff,
                                         ('argInfo', AnyKey('argname')),
                                         onlyDicts=True):
        doc = argDiff.get('doc')
        if (isinstance(doc, arguments.RemovedKey)
                or (isinstance(doc, ChangedKey)
                    and not doc.newVal)):
            del argDiff['doc']

################################################################################

# Can ignore

def delDiff(multiKey, diffsDict=None):
    dictsAndKeys = []
    if diffsDict is None:
        currentItem = diffs
    else:
        currentItem = diffsDict
    for piece in multiKey:
        dictsAndKeys.append((currentItem, piece))
        try:
            currentItem = currentItem[piece]
        except Exception:
            return

    for currentItem, piece in reversed(dictsAndKeys):
        del currentItem[piece]
        if currentItem:
            break

KNOWN_IGNORABLE = [
    # MFn.Type has a bunch of changes each year...
    ('MFn', 'enums', 'Type'),
    ('MFn', 'pymelEnums', 'Type'),
]

for multiKey in KNOWN_IGNORABLE:
    delDiff(multiKey)

################################################################################

# MFnDependencyNode.isNameLocked/setNameLocked haven't existed on the node
# since 2017 (though they still appeared in the xml in 2019). They never
# seem to have been in the official docs...

mfnDepDiffs = diffs.get('MFnDependencyNode', {})
methodDiffs = mfnDepDiffs.get('methods', {})
for methName in ('isNameLocked', 'setNameLocked'):
    methDiff = methodDiffs.get(methName)
    if isinstance(methDiff, arguments.RemovedKey):
        del methodDiffs[methName]
invertDiffs = mfnDepDiffs.get('invertibles', {})
if (invertDiffs.get(4) == {0: ChangedKey('setNameLocked', 'setUuid'),
                           1: ChangedKey('isNameLocked', 'uuid')}
        and invertDiffs.get(5) == RemovedKey(('setUuid', 'uuid'))):
    del invertDiffs[4]
    del invertDiffs[5]

################################################################################

# New subclasses

OK_NEW_VALS = {
    'MCreatorFunction': ['MCustomEvaluatorCreatorFunction',
                         'MTopologyEvaluatorCreatorFunction'],
    'nullptr': [None],
}

def isOkChange(val):
    if not isinstance(val, ChangedKey):
        return False
    if val.oldVal not in OK_NEW_VALS:
        return False
    return val.newVal in OK_NEW_VALS[val.oldVal]

arguments.deepPatch(diffs, isOkChange, returnNone)


################################################################################

# CAN IGNORE - 2021

CAN_IGNORE_2021 = [
    # The docstring for MFnAirfield got messed up
    ('MFnAirField', 'methods', 'setSpeed', 0, 'argInfo', 'value', 'doc'),
    # docstring got messed up
    ('MFnMesh', 'methods', 'create', 5, 'argInfo', 'edgeFaceDesc', 'doc'),
    ('MFnMesh', 'methods', 'create', 6, 'argInfo', 'edgeFaceDesc', 'doc'),
    # docstring changed
    ('MFnSubdNames', 'methods', 'baseFaceIndexFromId', 0, 'doc'),
    # This is a valid fix - MFnIkJoint::getPreferedAngle (the mispelled,
    # obsolete one) formerly had 'rotation' improperly marked as an in arg
    ('MFnIkJoint', 'methods', 'getPreferedAngle', 0, 'args', 0, 2),
    ('MFnIkJoint', 'methods', 'getPreferedAngle', 0, 'inArgs', 0),
    ('MFnIkJoint', 'methods', 'getPreferedAngle', 0, 'outArgs', 0),
    # A valid fix - 'const unsigned short' was formerly parsed (in the xml)
    # as a type of "const unsigned" and a name of "short"
    ('MFloatPoint', 'methods', '__imul__', 4, 'argInfo', 'factor', 'type'),
    ('MFloatPoint', 'methods', '__imul__', 4, 'args', 0, 1),
    ('MFloatPoint', 'methods', '__imul__', 4, 'types', 'factor'),
    ('MPoint', 'methods', '__imul__', 4, 'argInfo', 'factor', 'type'),
    ('MPoint', 'methods', '__imul__', 4, 'args', 0, 1),
    ('MPoint', 'methods', '__imul__', 4, 'types', 'factor'),
]
if versions.current() // 10000 == cacheversions['new']:
    for multiKey in CAN_IGNORE_2021:
        delDiff(multiKey)

################################################################################

# KNOWN PROBLEMS

# place to temporarily put issues that need fixing, but you want to filter

KNOWN_PROBLEMS_2021 = [
]

if versions.current() // 10000 == cacheversions['new']:
    for multiKey in KNOWN_PROBLEMS_2021:
        delDiff(multiKey)


################################################################################

# clean up any diff dicts that are now empty
def pruneEmpty(diffs):
    def isempty(arg):
        return isinstance(arg, (dict, list, tuple, set, types.NoneType)) and not arg

    def hasEmptyChildren(arg):
        if not isinstance(arg, dict):
            return False
        return any(isempty(child) for child in arg.values())

    def pruneEmptyChildren(arg):
        keysToDel = []
        for key, val in arg.items():
            if isempty(val):
                keysToDel.append(key)
        for key in keysToDel:
            del arg[key]
        return arg

    altered = True        

    while altered:
        diffs, altered = arguments.deepPatchAltered(diffs, hasEmptyChildren, pruneEmptyChildren)
    return diffs

# afterPrune = pruneEmpty({'foo': 7, 'bar': {5:None, 8:None}})
# print(afterPrune)
diffs = pruneEmpty(diffs)
diff_classes = sorted(diffs)

print('###########')
print("Num diffs: {}".format(len(diffs)))
print('###########')
print("diff_classes:")
for cls in diff_classes:
    print("  " + str(cls))
print('###########')
if len(diffs):
    print("first class diff:")
    print(diff_classes[0])
    pprint(diffs[diff_classes[0]])
else:
    print("no diffs left! hooray!")
print('###########')
