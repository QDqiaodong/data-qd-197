import argparse
import json
import sys
from .io import load,publish
from .service import process
from .preflight import run as preflight_run
from . import ledger

def _stock(command,argv):
    parser=argparse.ArgumentParser(prog=f'recipe-yield {command}')
    parser.add_argument('--ledger',required=True)
    if command!='stock-show':parser.add_argument('--source',required=True)
    args=parser.parse_args(argv)
    try:
        if command=='stock-init':
            ledger.init_ledger(args.ledger,load(args.source))
        elif command=='stock-show':
            print(json.dumps(ledger.show_ledger(args.ledger),ensure_ascii=False,sort_keys=True))
        else:
            sys.stdout.write(ledger.issue(args.ledger,args.source)+'\n')
    except (ValueError,OSError,UnicodeError,RecursionError) as e:
        print(f'error: {e}',file=sys.stderr);return 2
    return 0

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('stock-init','stock-show','stock-issue'):
        return _stock(argv[0],argv[1:])
    if argv and argv[0]=='label-preflight':
        parser=argparse.ArgumentParser(prog='recipe-yield label-preflight')
        parser.add_argument('--source',required=True)
        parser.add_argument('--output',required=True)
        args=parser.parse_args(argv[1:])
        try:
            result=preflight_run(load(args.source));publish(args.output,result['report'])
        except (ValueError,OSError,UnicodeError,RecursionError) as e:
            print(f'error: {e}',file=sys.stderr);return 2
        print(json.dumps(result['summary'],ensure_ascii=False,sort_keys=True));return 0
    parser=argparse.ArgumentParser(prog='recipe-yield')
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        result=process(load(args.source));publish(args.output,result)
    except (ValueError,OSError,UnicodeError,RecursionError) as e:
        print(f'error: {e}',file=sys.stderr);return 2
    print(json.dumps(result['summary'],ensure_ascii=False,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
