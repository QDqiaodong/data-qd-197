from fractions import Fraction

def expand(ingredients,recipes,ref,grams):
    result=[]
    def walk(recipe,amount,path):
        r=recipes[recipe];scale=amount/r['output']
        for index,(kind,key,net) in enumerate(r['components']):
            required=net*scale;trace=path+[f'{recipe}:{index}']
            if kind=='recipe':walk(key,required,trace)
            else:
                i=ingredients[key];gross=required/i['yield']
                result.append({'ingredient':key,'net':required,'gross':gross,'trim':gross-required,'cost':gross*i['price'],'tags':i['tags'],'path':trace})
                if len(result)>20000:raise ValueError('expanded component limit exceeded')
    walk(ref,grams,[])
    return result

def totals(lines):
    result={}
    for row in lines:
        r=result.setdefault(row['ingredient'],dict(net=Fraction(0),gross=Fraction(0),trim=Fraction(0),cost=Fraction(0)))
        for key in r:r[key]+=row[key]
    return [{'ingredient':key,**{k:str(v) for k,v in value.items()}} for key,value in sorted(result.items())]
