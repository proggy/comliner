# Change Log


## 0.4.0

- Python 3 compatibility
- remove dependency on h5obj, use vanilla h5py instead
- remove dependency on progress, use tqdm instead
- introduce submodule "dummy", replacing dependency on separate package
- switch dependency from easytable to clitable
- monkey-patch inspect.getargspec until inspect becomes Python 3 compatible
- change name to comliner

## 0.3.0

- output mapping is now a dictionary, just like input mapping
- output mapping can now contain references to metadata, like DATE and
    TIMINGS

