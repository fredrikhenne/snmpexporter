#!/usr/bin/env python2
import binascii
import collections
import logging
import time

import actions
import config
import snmp
import stage


class Annotator(object):
  """Annotation step where results are given meaningful labels."""

  LABEL_TYPES = set(['OCTETSTR', 'IPADDR'])
  ALLOWED_CHARACTERS = (
      '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
      '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~ ')

  def __init__(self):
    super(Annotator, self).__init__()
    self.mibcache = {}
    self._mibresolver = None

  @property
  def mibresolver(self):
    # Do the import here to not spam the terminal with netsnmp stuff
    if self._mibresolver is None:
      import mibresolver
      self._mibresolver = mibresolver
    return self._mibresolver

  def do_result(self, run, target, results, stats):
    annotations = config.get('annotator', 'annotations') or []

    # Calculate map to skip annotation if we're sure we're not going to annotate
    # TODO(bluecmd): This could be cached
    annotation_map = {}
    for annotation in annotations:
      for annotate in annotation['annotate']:
        # Add '.' to not match .1.2.3 if we want to annotate 1.2.30
        annotation_map[annotate + '.'] = annotation['with']

    labelification = set(
        [x + '.' for x in config.get('annotator', 'labelify') or []])

    # Calculate annotator map
    split_oid_map = collections.defaultdict(dict)
    for (oid, ctxt), result in results.iteritems():
      # We only support the last part of an OID as index for annotations
      key, index = oid.rsplit('.', 1)
      key += '.'
      split_oid_map[(key, ctxt)][index] = result.value

    annotated_results = {}
    for (oid, ctxt), result in results.iteritems():
      # Record some stats on how long time it took to get this metric
      elapsed = (time.time() - target.timestamp) * 1000 * 1000
      labels = {}
      vlan = None

      # TODO(bluecmd): If we support more contexts we need to be smarter here
      if not ctxt is None:
        vlan = ctxt

      name = self.mibcache.get(oid, None)
      if name is None:
        name = self.mibresolver.resolve(oid)
        self.mibcache[oid] = name

      if name is None:
        logging.warning('Failed to look up OID %s, ignoring', oid)
        continue

      if not '::' in name:
        logging.warning('OID %s resolved to %s (no MIB), ignoring', oid, name)
        continue

      mib, part = name.split('::', 1)
      obj, index = part.split('.', 1) if '.' in part else (part, None)

      labels = {}
      if not vlan is None:
        labels['vlan'] = vlan
      labels.update(
          self.annotate(
            oid, ctxt, annotation_map, split_oid_map, results))

      # Handle labelification
      if oid[:-len(index)] in labelification:
        # Skip empty strings or non-strings that are up for labelification
        if result.value == '' or result.type not in self.LABEL_TYPES:
          continue
        labels['value'] = self.string_to_label_value(result.value)
        labels['hex'] = binascii.hexlify(result.value)
        result = snmp.ResultTuple('1', 'INTEGER')

      annotated_results[(oid, vlan)] = actions.AnnotatedResultEntry(
          result, mib, obj, index, labels)

    yield actions.AnnotatedResult(target, annotated_results, stats)
    logging.debug('Annotation completed for %d metrics for %s',
        len(annotated_results), target.host)

  def annotate(self, oid, ctxt, annotation_map, split_oid_map, results):
    for key in annotation_map:
      if oid.startswith(key):
        break
    else:
      return {}

    # We only support the last part of an OID as index for annotations
    _, index = oid.rsplit('.', 1)
    labels = {}

    for label, annotation_path in annotation_map[key].iteritems():
      # Parse the annotation path
      annotation_keys = [x.strip() + '.' for x in annotation_path.split('>')]

      # Jump across the path seperated like:
      # OID.idx:value1
      # OID2.value1:value2
      # OID3.value3:final
      # label=final
      value = index
      for annotation_key in annotation_keys:
        # Try to associate with context first
        part = split_oid_map.get((annotation_key, ctxt), None)
        if not part:
          # Fall back to the global context
          part = split_oid_map.get((annotation_key, None), None)
          # Do not allow going back into context when you have jumped into
          # the global one.
          # TODO(bluecmd): I have no reason *not* to support this more than
          # it feels like an odd behaviour and not something I would be
          # expecting the software to do, so let's not do that unless we find
          # a usecase in the future.
          ctxt = None
          if not part:
            continue
        value = part.get(value, None)

      if not value:
        continue
      labels[label] = value.replace('"', '\\"')
    return labels

  def string_to_label_value(self, value):
    value = ''.join(x for x in value.strip() if x in self.ALLOWED_CHARACTERS)
    return value.strip()


if __name__ == '__main__':
  annotator = stage.Stage(Annotator())
  annotator.listen(actions.Result)
  annotator.run()
