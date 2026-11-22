from fractions import Fraction
from .model import parse
from .expand import expand,totals

def process(data):
    ingredients,recipes,orders=parse(data);all_lines=[];results=[]
    for key,ref,grams in orders:
        lines=expand(ingredients,recipes,ref,grams);all_lines.extend(lines)
        cost=sum((r['cost'] for r in lines),Fraction(0))
        evidence=[{k:(str(v) if isinstance(v,Fraction) else v) for k,v in r.items()} for r in lines]
        results.append({'id':key,'recipe':ref,'output_grams':str(grams),'cost':str(cost),'declarations':sorted({t for r in lines for t in r['tags']}),'evidence':evidence})
    purchases=totals(all_lines)
    return {'orders':results,'purchases':purchases,'summary':{'orders':len(results),'ingredients':len(purchases),'cost':str(sum((r['cost'] for r in all_lines),Fraction(0)))}}
