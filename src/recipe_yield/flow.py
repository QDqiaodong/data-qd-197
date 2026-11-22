from fractions import Fraction

class Dinic:
    # Exact max-flow over integer or rational capacities; Dinic's phase
    # bound does not depend on capacity values, so termination is guaranteed
    # for any exact numeric type. zero selects the additive identity of the
    # capacity type in use (0 for ints, Fraction(0) for rationals).
    def __init__(self,n,zero=Fraction(0)):
        self.n=n
        self.zero=zero
        self.adj=[[]for _ in range(n)]
    def add_edge(self,u,v,cap):
        self.adj[u].append([v,cap,len(self.adj[v])])
        self.adj[v].append([u,self.zero,len(self.adj[u])-1])
    def max_flow(self,s,t,limit=None):
        # Resumable: edges added later can be followed by another max_flow
        # call, which continues from the current residual graph. limit caps
        # the total value pushed by this call; None means unbounded.
        total=self.zero;n=self.n
        while limit is None or total<limit:
            level=[-1]*n;level[s]=0;queue=[s]
            for u in queue:
                for e in self.adj[u]:
                    if e[1]>0 and level[e[0]]<0:level[e[0]]=level[u]+1;queue.append(e[0])
            if level[t]<0:return total
            it=[0]*n
            def dfs(u,f):
                # f is None only at the source (unbounded); it becomes a
                # concrete capacity after traversing the first edge.
                if u==t:return f
                while it[u]<len(self.adj[u]):
                    e=self.adj[u][it[u]]
                    if e[1]>0 and level[e[0]]==level[u]+1:
                        d=dfs(e[0],e[1] if f is None else min(f,e[1]))
                        if d is None:d=e[1]
                        if d>0:
                            e[1]-=d;self.adj[e[0]][e[2]][1]+=d
                            return d
                    it[u]+=1
                return self.zero
            while limit is None or total<limit:
                pushed=dfs(s,None if limit is None else limit-total)
                if not pushed:break
                total+=pushed
        return total
